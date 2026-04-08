import json

import db
from llm import LLMClient


SECTIONS = [
    ("overview", "High-level project overview: what the repository appears to be, its purpose, and main technologies inferred from commits and files."),
    ("architecture_evolution", "How the architecture evolved across time. Reference specific eras and pivotal commits."),
    ("main_modules", "Primary modules / subsystems of the codebase and their roles."),
    ("critical_decisions", "Key architectural or design decisions and the reasoning behind them."),
    ("branch_history", "Notable branches, merge patterns, and how collaboration happened."),
    ("major_refactors", "Significant refactors, rewrites, or migrations."),
    ("risks", "Fragile areas, debt hotspots, and risks onboarding engineers should watch."),
    ("onboarding", "Practical onboarding guide: where to start reading, what to run, key files."),
    ("timeline", "Chronological bullet list of defining moments in the codebase's history."),
]


def _build_context(mission_id: str) -> str:
    repo = db.get_repo(mission_id) or {}
    nodes = db.get_knowledge_nodes(mission_id)
    touches = db.file_touch_counts(mission_id, min_touches=3, limit=30)
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
        "hot_files": [{"path": t["path"], "touches": t["touches"]} for t in touches[:20]],
        "knowledge_nodes": [
            {
                "id": n["id"],
                "kind": n["kind"],
                "title": n["title"],
                "summary": n["summary"],
                "first_date": n.get("first_date"),
                "last_date": n.get("last_date"),
                "member_count": len(n.get("member_shas", [])),
                "sample_shas": [s[:12] for s in n.get("member_shas", [])[:6]],
            }
            for n in nodes
        ],
    }
    return json.dumps(ctx, ensure_ascii=False)


def write_all(mission_id: str, llm: LLMClient, progress_cb=None) -> int:
    context = _build_context(mission_id)
    written = 0
    for i, (section, instruction) in enumerate(SECTIONS):
        prompt_ctx = f"INSTRUCTION: {instruction}\n\nREPO STATE:\n{context}"
        try:
            result = llm.write_section(section, prompt_ctx)
            db.save_report_section(mission_id, section, result.get("content", ""), result.get("refs", []))
            written += 1
        except Exception as e:
            print(f"[reporter] section {section} failed: {e}")
            db.save_report_section(
                mission_id,
                section,
                f"(Section generation failed: {e})",
                [],
            )
        if progress_cb:
            progress_cb(i + 1, len(SECTIONS))
    return written
