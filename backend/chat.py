import json
import re

import db
import embed
import git_ingest
from config import settings
from llm import LLMClient


# ---- intent detection -------------------------------------------------------

_ENTITY_EXPLAIN_RE = re.compile(
    r'\b(?:what does|how does|explain|describe|what is|what\'s|show me|tell me about)\b',
    re.IGNORECASE,
)
_ENTITY_ORIGIN_RE = re.compile(
    r'\b(?:why does|why was|why is|why exists?|who wrote|who added|when was added|purpose of|reason for)\b',
    re.IGNORECASE,
)
_EVOLUTION_RE = re.compile(
    r'\b(?:how has|history of|changes to|evolved|evolution|changed over|when did)\b',
    re.IGNORECASE,
)
_ONBOARDING_RE = re.compile(
    r'\b(?:where to start|what to read|how to set up|get started|onboard|new developer|where do i start|first steps|reading order)\b',
    re.IGNORECASE,
)


def _detect_intent(question: str) -> str:
    if _ENTITY_EXPLAIN_RE.search(question):
        return "entity_explain"
    if _ENTITY_ORIGIN_RE.search(question):
        return "entity_origin"
    if _EVOLUTION_RE.search(question):
        return "file_evolution"
    if _ONBOARDING_RE.search(question):
        return "onboarding"
    return "general"


def _extract_entity_name(question: str) -> str | None:
    """Try to extract a code symbol name from the question text."""
    # Backtick-quoted identifier: `func_name`
    m = re.search(r'`(\w+)`', question)
    if m:
        return m.group(1)
    # snake_case identifiers (two or more words joined by underscore)
    m = re.search(r'\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b', question)
    if m:
        return m.group(1)
    # camelCase identifiers
    m = re.search(r'\b([a-z]+[A-Z][a-zA-Z0-9]+)\b', question)
    if m:
        return m.group(1)
    return None


# ---- context formatters -----------------------------------------------------

SYSTEM_PROMPT = (
    "You are an elite Principal Software Engineer and expert Technical Onboarding Tutor for this codebase. "
    "Your goal is to transfer deep knowledge to new developers. Always explain exactly how the code works, "
    "what specific files do, and deeply analyze 'why' a feature exists based on the architecture. "
    "Answer from the evidence blocks below, but synthesize into a complete, useful answer. "
    "Cite using inline tags: [sha:abcdef1234], [branch:NAME], [file:path/to/file], [fn:path::name]. "
    "If evidence is weak or missing, explain what you know and what is missing gracefully."
)


def _fmt_entity_evidence(entities: list[dict]) -> str:
    blocks: list[str] = []
    for e in entities:
        parts = [f"[ENTITY {e['id']} kind:{e['kind']}]"]
        if e.get("signature"):
            parts.append(f"Signature: {e['signature']}")
        if e.get("docstring"):
            parts.append(f"Docstring: {e['docstring']}")
        if e.get("llm_summary"):
            parts.append(f"What it does: {e['llm_summary']}")
        if e.get("llm_why"):
            parts.append(f"Why it exists: {e['llm_why']}")
        if e.get("code_snippet"):
            parts.append(f"Code:\n{(e['code_snippet'] or '')[:300]}")
        blocks.append("\n".join(parts))
    return "\n\n".join(blocks)


def _fmt_vector_evidence(hits: list[dict], diffs: dict[str, str]) -> str:
    blocks: list[str] = []
    for h in hits:
        src = h.get("source")
        ident = h.get("id") or ""
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
        elif src == "entities":
            header = f"[ENTITY id:{ident} kind:{meta.get('kind','')} path:{meta.get('path','')}]"
            block = f"{header}\n{doc}"
        else:
            block = f"[{src}]\n{doc}"
        blocks.append(block)
    return "\n\n".join(blocks)


def _collect_citations(hits: list[dict], entity_ids: list[str]) -> dict:
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
    return {"shas": shas, "files": files, "branches": [], "clusters": clusters, "entities": entity_ids}


# ---- main entry point -------------------------------------------------------

async def chat_stream(mission_id: str, question: str, history: list[dict]):
    repo = db.get_repo(mission_id) or {}
    clone_path = repo.get("clone_path") or ""
    intent = _detect_intent(question)
    entity_name = _extract_entity_name(question)

    # Stage 1: entity-specific lookup (for code-level questions)
    entity_context = ""
    resolved_entities: list[dict] = []
    if intent in ("entity_explain", "entity_origin") and entity_name:
        candidates = db.list_code_entities(mission_id, limit=1000)
        exact = [e for e in candidates if e["name"].lower() == entity_name.lower()]
        partial = [e for e in candidates if entity_name.lower() in e["name"].lower()] if not exact else []
        resolved_entities = (exact or partial)[:4]
        if resolved_entities:
            entity_context = _fmt_entity_evidence(resolved_entities)

            # For origin intent: attach introducing commit if available
            if intent == "entity_origin":
                for e in resolved_entities:
                    sha = e.get("introduced_sha")
                    if sha:
                        commit_detail = db.get_commit_detail(mission_id, sha)
                        if commit_detail:
                            entity_context += (
                                f"\n\n[INTRODUCING COMMIT sha:{sha[:12]}]\n"
                                f"Title: {commit_detail.get('title') or ''}\n"
                                f"Why: {commit_detail.get('why') or ''}\n"
                                f"Impact: {commit_detail.get('impact') or ''}"
                            )

    # Stage 2: vector search across all collections
    hits = embed.search(mission_id, question, k=settings.chat_top_k)

    # Attach diffs to top commit hits
    diffs: dict[str, str] = {}
    for h in [h for h in hits if h.get("source") == "commits"][: settings.chat_diff_attach]:
        try:
            diffs[h["id"]] = git_ingest.read_diff(clone_path, h["id"], max_bytes=1200)
        except Exception:
            pass

    vector_evidence = _fmt_vector_evidence(hits, diffs) or "(no additional evidence retrieved)"

    # Assemble final system prompt
    evidence_section = ""
    if entity_context:
        evidence_section += f"CODE ENTITIES:\n{entity_context}\n\n"
    evidence_section += f"VECTOR EVIDENCE:\n{vector_evidence}"

    system = (
        SYSTEM_PROMPT
        + f"\n\nREPO: {repo.get('url','')} (default branch: {repo.get('default_branch','?')})"
        + f"\n\nINTENT: {intent}"
        + f"\n\nEVIDENCE:\n{evidence_section}"
    )

    messages = [{"role": m["role"], "content": m["content"]} for m in (history or [])[-6:]]
    messages.append({"role": "user", "content": question})

    llm = LLMClient()
    for chunk in llm.chat_stream(system, messages):
        yield {"event": "message", "data": chunk}

    entity_ids = [e["id"] for e in resolved_entities]
    yield {"event": "citations", "data": json.dumps(_collect_citations(hits, entity_ids))}
