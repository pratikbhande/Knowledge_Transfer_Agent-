import json

import db
import embed
import git_ingest
from config import settings
from llm import LLMClient


SYSTEM_PROMPT = (
    "You are the Knowledge Tree assistant for a git repository. "
    "Answer only from the evidence blocks below. "
    "Cite using inline tags: [sha:abcdef1234], [branch:NAME], [file:path/to/file]. "
    "Distinguish fact (grounded in a commit or file) from inference. "
    "If evidence is weak or missing, say so in one sentence instead of guessing. "
    "Keep answers tight; prefer short paragraphs and bullets."
)


def _format_evidence(hits: list[dict], diffs: dict[str, str]) -> str:
    blocks: list[str] = []
    for h in hits:
        src = h.get("source")
        ident = h.get("id")
        doc = (h.get("document") or "").strip()
        meta = h.get("metadata") or {}
        if src == "commits":
            header = f"[COMMIT sha:{ident[:12]} date:{meta.get('date','')[:10]} author:{meta.get('author','')}]"
            tail = diffs.get(ident, "")
            block = f"{header}\n{doc}"
            if tail:
                block += f"\nDIFF:\n{tail}"
        elif src == "clusters":
            header = f"[CLUSTER id:{ident} kind:{meta.get('kind','theme')}]"
            block = f"{header}\n{doc}"
        elif src == "files":
            header = f"[FILE path:{ident} touches:{meta.get('touches',0)}]"
            block = f"{header}\n{doc}"
        else:
            block = f"[{src}]\n{doc}"
        blocks.append(block)
    return "\n\n".join(blocks)


def _collect_citations(hits: list[dict]) -> dict:
    shas: list[str] = []
    files: list[str] = []
    clusters: list[str] = []
    for h in hits:
        src = h.get("source")
        if src == "commits":
            shas.append(h["id"])
        elif src == "files":
            files.append(h["id"])
        elif src == "clusters":
            clusters.append(h["id"])
    return {"shas": shas, "files": files, "branches": [], "clusters": clusters}


async def chat_stream(mission_id: str, question: str, history: list[dict]):
    repo = db.get_repo(mission_id) or {}
    clone_path = repo.get("clone_path") or ""

    hits = embed.search(mission_id, question, k=settings.chat_top_k)

    diffs: dict[str, str] = {}
    commit_hits = [h for h in hits if h.get("source") == "commits"]
    for h in commit_hits[: settings.chat_diff_attach]:
        sha = h["id"]
        try:
            diffs[sha] = git_ingest.read_diff(clone_path, sha, max_bytes=1200)
        except Exception:
            pass

    evidence = _format_evidence(hits, diffs) or "(no evidence retrieved)"

    system = (
        SYSTEM_PROMPT
        + f"\n\nREPO: {repo.get('url','')} (default branch: {repo.get('default_branch','?')})"
        + f"\n\nEVIDENCE:\n{evidence}"
    )

    messages = [{"role": m["role"], "content": m["content"]} for m in (history or [])[-5:]]
    messages.append({"role": "user", "content": question})

    llm = LLMClient()
    for chunk in llm.chat_stream(system, messages):
        yield {"event": "message", "data": chunk}

    citations = _collect_citations(hits)
    yield {"event": "citations", "data": json.dumps(citations)}
