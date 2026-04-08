from collections import defaultdict
from datetime import datetime

import db
from llm import LLMClient


def _month_key(date_str: str) -> str:
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00")).strftime("%Y-%m")
    except Exception:
        return "unknown"


def _finalize_nodes(nodes: list[dict], summary_index: dict[str, dict]) -> list[dict]:
    """Attach first/last date from member commits and drop invalid SHAs."""
    clean: list[dict] = []
    for n in nodes:
        members = [s for s in n.get("member_shas", []) if _lookup(s, summary_index)]
        if not members:
            continue
        resolved = [_lookup(s, summary_index)["sha"] for s in members]
        dates = [summary_index_entry_date(summary_index, s) for s in members]
        dates = sorted(d for d in dates if d)
        clean.append(
            {
                "id": n["id"],
                "kind": n.get("kind", "theme"),
                "title": n["title"],
                "summary": n.get("summary", ""),
                "member_shas": resolved,
                "first_date": dates[0] if dates else None,
                "last_date": dates[-1] if dates else None,
            }
        )
    return clean


def _lookup(sha_prefix: str, index: dict[str, dict]) -> dict | None:
    if sha_prefix in index:
        return index[sha_prefix]
    for full, entry in index.items():
        if full.startswith(sha_prefix):
            return entry
    return None


def summary_index_entry_date(index: dict[str, dict], sha_prefix: str) -> str | None:
    entry = _lookup(sha_prefix, index)
    return entry.get("date") if entry else None


def cluster_knowledge_nodes(mission_id: str, llm: LLMClient) -> int:
    summaries = db.all_analyses(mission_id)
    if not summaries:
        return 0

    index = {s["sha"]: s for s in summaries}

    if len(summaries) <= 400:
        nodes = llm.cluster(summaries)
        final = _finalize_nodes(nodes, index)
        db.save_knowledge_nodes(mission_id, final)
        return len(final)

    # Large repo: bucket by month, cluster within bucket, then one merge pass.
    buckets: dict[str, list[dict]] = defaultdict(list)
    for s in summaries:
        buckets[_month_key(s.get("date") or "")].append(s)

    partial_nodes: list[dict] = []
    for key, bucket in buckets.items():
        if not bucket:
            continue
        chunk_nodes = llm.cluster(bucket)
        for n in chunk_nodes:
            n["id"] = f"{key}:{n['id']}"
        partial_nodes.extend(chunk_nodes)

    # Merge pass: ask the LLM to deduplicate / merge themes across buckets.
    merged_input = [
        {
            "sha": n["id"],  # reuse sha field as a pseudo-id to reuse the prompt shape
            "date": "",
            "title": n["title"],
            "why": n["summary"],
            "tags": [],
            "modules": [],
        }
        for n in partial_nodes
    ]
    merged = llm.cluster(merged_input)
    # Re-map member_shas from merged node ids back to the underlying commit shas.
    node_by_pseudo = {n["id"]: n for n in partial_nodes}
    final_nodes: list[dict] = []
    for i, m in enumerate(merged):
        real_members: list[str] = []
        for pseudo in m.get("member_shas", []):
            source = node_by_pseudo.get(pseudo)
            if source:
                real_members.extend(source.get("member_shas", []))
        if not real_members:
            continue
        final_nodes.append(
            {
                "id": f"merged-{i}",
                "kind": m.get("kind", "theme"),
                "title": m["title"],
                "summary": m.get("summary", ""),
                "member_shas": list(dict.fromkeys(real_members)),
            }
        )

    final = _finalize_nodes(final_nodes, index)
    db.save_knowledge_nodes(mission_id, final)
    return len(final)
