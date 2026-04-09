import json

import db
from llm import LLMClient


# Sections grouped into 4 batches → 4 LLM calls instead of 13 (70% cost saving)
SECTION_BATCHES = [
    [
        ("overview", "Comprehensive project overview: what the repository is, its primary purpose, and its end-to-end functionality."),
        ("folder_structure", "Complete folder and file structure. For EVERY important file write one sentence on its exact role. Present as a hierarchical list. Include all files from the hot_files and key_functions context."),
        ("architecture_evolution", "How the architecture evolved across time. Reference pivotal commits and eras of development."),
    ],
    [
        ("core_components_and_files", "Detailed breakdown of the most critical files: exactly what each file does, how the code flows through it, and why it is important for a new developer."),
        ("function_inventory", "Inventory of the 15-25 most important public functions and classes. For each: name, file path, what it does (1-2 sentences), why it exists. Draw from key_functions in the context."),
        ("data_flow", "End-to-end data flow. Trace a typical request from entry point through every function called, what is stored, and what is returned. Use concrete function names from the codebase."),
    ],
    [
        ("entry_points", "All main entry points (server startup, CLI commands, main functions). For each, trace the first 3-5 functions called in the call chain."),
        ("critical_decisions", "Deep analysis of key architectural or design decisions: why specific features were built the way they were."),
        ("risks", "Fragile areas, debt hotspots, and risks onboarding engineers should watch out for."),
    ],
    [
        ("getting_started", "Step-by-step reading order for a new developer. List exactly which files to read first, second, third — and WHY. Include which functions to understand first. Concrete enough to follow on day 1."),
        ("branch_history", "Notable branches, merge patterns, and how collaboration happened."),
        ("major_refactors", "Significant refactors, rewrites, or migrations. Explain the technical debt eliminated."),
        ("timeline", "Chronological bullet list of defining moments in the codebase's history."),
    ],
]


def _build_context(mission_id: str) -> str:
    repo = db.get_repo(mission_id) or {}
    nodes = db.get_knowledge_nodes(mission_id)
    touches = db.file_touch_counts(mission_id, min_touches=2, limit=40)
    entities = db.list_code_entities(mission_id, limit=300)
    file_entities = {e["path"]: e for e in entities if e["kind"] == "file"}
    fn_entities = [
        e for e in entities
        if e["kind"] in ("function", "class", "method") and e.get("llm_summary")
    ]

    with db.open_db(mission_id) as conn:
        branches = [dict(r) for r in conn.execute("SELECT name, head_sha, is_default FROM branches LIMIT 50").fetchall()]
        commit_count = conn.execute("SELECT COUNT(*) FROM commits").fetchone()[0]
        date_range = conn.execute("SELECT MIN(date), MAX(date) FROM commits").fetchone()

    ctx = {
        "repo": {
            "url": repo.get("url"),
            "default_branch": repo.get("default_branch"),
            "head_sha": repo.get("head_sha"),
            "commit_count": commit_count,
            "first_commit": date_range[0],
            "last_commit": date_range[1],
        },
        "branches": branches[:20],
        "hot_files": [
            {
                "path": t["path"],
                "touches": t["touches"],
                "llm_summary": file_entities.get(t["path"], {}).get("llm_summary"),
                "recent_commits": [
                    c.get("title")
                    for c in db.top_commits_for_file(mission_id, t["path"], limit=4)
                ],
            }
            for t in touches[:30]
        ],
        "key_functions": [
            {
                "id": e["id"],
                "name": e["name"],
                "path": e["path"],
                "signature": e.get("signature"),
                "llm_summary": e.get("llm_summary"),
                "llm_why": e.get("llm_why"),
                "introduced_sha": e.get("introduced_sha"),
            }
            for e in fn_entities[:50]
        ],
        "knowledge_nodes": [
            {
                "id": n["id"],
                "kind": n["kind"],
                "title": n["title"],
                "summary": n["summary"],
                "first_date": n.get("first_date"),
                "last_date": n.get("last_date"),
                "member_count": len(n.get("member_shas", [])),
            }
            for n in nodes
        ],
    }
    return json.dumps(ctx, ensure_ascii=False)


def write_all(mission_id: str, llm: LLMClient, progress_cb=None) -> int:
    context = _build_context(mission_id)
    written = 0
    total_sections = sum(len(batch) for batch in SECTION_BATCHES)
    done_so_far = 0

    for batch in SECTION_BATCHES:
        try:
            results = llm.write_sections_batch(batch, context)
            for r in results:
                db.save_report_section(mission_id, r["section"], r["content"], r["refs"])
                written += 1
        except Exception as e:
            print(f"[reporter] batch failed: {e}")
            # Fall back: save placeholder for each section in failed batch
            for key, _ in batch:
                db.save_report_section(
                    mission_id, key, f"(Section generation failed: {e})", []
                )
        done_so_far += len(batch)
        if progress_cb:
            progress_cb(done_so_far, total_sections)

    return written
