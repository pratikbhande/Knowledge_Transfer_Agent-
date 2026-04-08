import re

import db
import git_ingest
from config import HOTSPOT_KEYWORDS, HOTSPOT_PATHS, settings
from llm import LLMClient


_TYPE_RE = re.compile(r"^(feat|fix|refactor|docs|test|chore|build|ci|perf|style|merge)\b", re.IGNORECASE)
_HOTSPOT_RE = re.compile("|".join(re.escape(k) for k in HOTSPOT_KEYWORDS), re.IGNORECASE)


def classify(message: str) -> str:
    m = _TYPE_RE.match(message.strip())
    if m:
        return m.group(1).lower()
    low = message.lower()
    if low.startswith("merge"):
        return "merge"
    if any(w in low for w in ("fix", "bug", "patch")):
        return "fix"
    if any(w in low for w in ("add", "introduce", "implement", "support")):
        return "feat"
    if any(w in low for w in ("refactor", "cleanup", "rework")):
        return "refactor"
    if any(w in low for w in ("doc", "readme")):
        return "docs"
    if any(w in low for w in ("test", "spec")):
        return "test"
    return "chore"


def _is_hotspot_path(path: str) -> bool:
    if path in HOTSPOT_PATHS:
        return True
    base = path.split("/")[-1]
    if base in HOTSPOT_PATHS:
        return True
    if path.startswith(".github/workflows"):
        return True
    return False


def select_key_commits(mission_id: str) -> int:
    """Mark key commits using deterministic heuristics. Returns count."""
    key: list[str] = []
    with db.open_db(mission_id) as conn:
        rows = conn.execute(
            "SELECT sha, message, is_merge, files_changed, insertions, deletions, seq FROM commits"
        ).fetchall()
        file_map: dict[str, list[str]] = {}
        fr = conn.execute("SELECT sha, path FROM commit_files").fetchall()
        for f in fr:
            file_map.setdefault(f["sha"], []).append(f["path"])

    stride = max(1, settings.narrative_sampling_stride)
    for r in rows:
        sha = r["sha"]
        msg = r["message"] or ""
        total_lines = int(r["insertions"]) + int(r["deletions"])
        reasons = 0
        if r["is_merge"]:
            reasons += 1
        if int(r["files_changed"]) >= settings.key_commit_file_threshold:
            reasons += 1
        if total_lines >= settings.key_commit_line_threshold:
            reasons += 1
        if _HOTSPOT_RE.search(msg):
            reasons += 1
        if any(_is_hotspot_path(p) for p in file_map.get(sha, [])):
            reasons += 1
        if (int(r["seq"] or 0) % stride) == 0:
            reasons += 1
        if reasons > 0:
            key.append(sha)

    db.mark_key(mission_id, key)
    return len(key)


def classify_all(mission_id: str) -> None:
    with db.open_db(mission_id) as conn:
        rows = conn.execute("SELECT sha, message FROM commits").fetchall()
        updates = [(classify(r["message"] or ""), r["sha"]) for r in rows]
        conn.executemany("UPDATE commits SET decision_type=? WHERE sha=?", updates)


def run_summarization(mission_id: str, llm: LLMClient, clone_path: str, progress_cb=None) -> None:
    total_key = db.count_key_commits(mission_id)
    if total_key == 0:
        return
    done = 0
    while True:
        batch = db.get_pending_key_commits(mission_id, settings.summarize_batch_size)
        if not batch:
            break
        for c in batch:
            if c.get("files_changed", 0) <= 20 and c.get("insertions", 0) + c.get("deletions", 0) <= 400:
                c["diff"] = git_ingest.read_diff(clone_path, c["sha"], max_bytes=1200)
            else:
                c["diff"] = ""
        try:
            items = llm.summarize_commits(batch)
        except Exception as e:
            print(f"[summarizer] batch error: {e}; retrying individually")
            items = []
            for single in batch:
                try:
                    items.extend(llm.summarize_commits([single]))
                except Exception as se:
                    print(f"[summarizer] single failed: {se}")
                    items.append(
                        {
                            "sha": single["sha"],
                            "title": (single["message"] or "").splitlines()[0][:80],
                            "why": "",
                            "impact": "",
                            "modules": [],
                            "tags": [single.get("decision_type") or "unknown"],
                            "risk": "",
                            "confidence": 0.3,
                        }
                    )
        db.save_analysis(mission_id, items)
        done += len(batch)
        if progress_cb:
            progress_cb(done, total_key)
