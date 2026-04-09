"""
code_analyzer.py — LLM-powered deep analysis of top hot files.

Cost-efficient: analyzes 5 files per LLM call instead of 1.
Analyzes top 10 most-changed files → only 2 LLM calls total.
Writes llm_summary + llm_why back to code_entities in DB.
"""

import db
import git_ingest
from llm import LLMClient

_MAX_FILES = 10   # top N hot files to analyze
_BATCH_SIZE = 5   # files per LLM call → _MAX_FILES / _BATCH_SIZE = 2 LLM calls


def run_code_analysis(mission_id: str, llm: LLMClient) -> int:
    """
    Analyze top hot files in batches of 5. Returns count of files analyzed.
    Total LLM calls: ceil(_MAX_FILES / _BATCH_SIZE) = 2.
    """
    repo = db.get_repo(mission_id)
    if not repo:
        return 0
    clone_path = repo["clone_path"]

    hot_files = db.file_touch_counts(mission_id, min_touches=2, limit=_MAX_FILES)
    enriched = 0

    # Build list of {path, content, recent_commits} for batch call
    file_inputs: list[dict] = []
    for t in hot_files:
        path = t["path"]
        content = git_ingest.read_file_at_head(clone_path, path)
        if not content or len(content) < 30:
            continue
        top = db.top_commits_for_file(mission_id, path, limit=5)
        commit_titles = [c.get("title") or "" for c in top if c.get("title")]
        file_inputs.append({
            "path": path,
            "content": content,
            "recent_commits": commit_titles,
        })

    # Process in batches of _BATCH_SIZE
    for i in range(0, len(file_inputs), _BATCH_SIZE):
        batch = file_inputs[i : i + _BATCH_SIZE]
        try:
            results = llm.analyze_files_batch(batch)
        except Exception as e:
            print(f"[code_analyzer] batch {i // _BATCH_SIZE + 1} failed: {e}")
            continue

        for result in results:
            path = result.get("path", "")
            if not path:
                continue

            # Update file-level entity
            file_id = f"file:{path}"
            db.update_entity_llm(mission_id, file_id, result["summary"], result["why"])

            # Update function/class entities for this file
            for fn in result.get("key_functions", []):
                fn_name = fn.get("name", "")
                if not fn_name:
                    continue
                # Try function prefix, then class prefix
                for prefix in ("fn", "cls"):
                    entity_id = f"{prefix}:{path}::{fn_name}"
                    if db.get_code_entity(mission_id, entity_id):
                        db.update_entity_llm(
                            mission_id,
                            entity_id,
                            fn.get("purpose", ""),
                            fn.get("why", ""),
                        )
                        break

            enriched += 1

    return enriched
