import json
import re
from typing import Iterator

import anthropic
import openai

from config import settings


class LLMUnavailable(RuntimeError):
    pass


class LLMClient:
    def __init__(self) -> None:
        self.ant = anthropic.Anthropic(api_key=settings.anthropic_api_key) if settings.anthropic_api_key else None
        self.oai = openai.OpenAI(api_key=settings.openai_api_key) if settings.openai_api_key else None

    # ---------- public API ----------

    def summarize_commits(self, batch: list[dict]) -> list[dict]:
        """Batched structured-JSON summarization. Returns one entry per input commit."""
        if not batch:
            return []
        prompt = _build_summarize_prompt(batch)
        data = self._json_call(
            system=_SUMMARIZE_SYSTEM,
            user=prompt,
            max_tokens=1800,
        )
        items = data.get("results", []) if isinstance(data, dict) else []
        by_sha = {str(i.get("sha", "")): i for i in items if isinstance(i, dict)}
        out: list[dict] = []
        for c in batch:
            sha = c["sha"]
            i = by_sha.get(sha) or by_sha.get(sha[:7]) or {}
            out.append(
                {
                    "sha": sha,
                    "title": _clip(i.get("title") or c["message"].splitlines()[0], 80),
                    "why": _clip(i.get("why") or "", 200),
                    "impact": _clip(i.get("impact") or "", 160),
                    "modules": _as_list(i.get("modules")),
                    "tags": _as_list(i.get("tags")),
                    "risk": _clip(i.get("risk") or "", 80),
                    "confidence": float(i.get("confidence") or 0.5),
                }
            )
        return out

    def cluster(self, summaries: list[dict]) -> list[dict]:
        if not summaries:
            return []
        compact = [
            {
                "sha": s["sha"][:12],
                "date": s.get("date", "")[:10],
                "title": s.get("title") or "",
                "why": s.get("why") or "",
                "tags": s.get("tags") or [],
                "modules": (s.get("modules") or [])[:4],
            }
            for s in summaries
        ]
        data = self._json_call(
            system=_CLUSTER_SYSTEM,
            user="COMMITS:\n" + json.dumps(compact, ensure_ascii=False),
            max_tokens=2500,
        )
        nodes = data.get("nodes", []) if isinstance(data, dict) else []
        clean: list[dict] = []
        for n in nodes:
            if not isinstance(n, dict):
                continue
            clean.append(
                {
                    "id": str(n.get("id") or n.get("title") or "node"),
                    "kind": str(n.get("kind") or "theme"),
                    "title": _clip(n.get("title") or "Untitled", 80),
                    "summary": _clip(n.get("summary") or "", 400),
                    "member_shas": [str(x)[:40] for x in (n.get("member_shas") or [])],
                }
            )
        return clean

    def write_section(self, section: str, context: str) -> dict:
        prompt = (
            f"SECTION: {section}\n"
            "Write the section in 2-5 short paragraphs or bullet lists. "
            "Use inline tags [sha:HASH], [branch:NAME], [file:PATH] when grounding claims.\n\n"
            f"CONTEXT:\n{context}\n"
        )
        data = self._json_call(
            system=_REPORT_SYSTEM,
            user=prompt,
            max_tokens=1400,
        )
        content = ""
        refs: list[dict] = []
        if isinstance(data, dict):
            content = str(data.get("content") or "")
            raw_refs = data.get("refs") or []
            if isinstance(raw_refs, list):
                for r in raw_refs:
                    if isinstance(r, dict):
                        refs.append({k: v for k, v in r.items() if k in ("sha", "branch", "file")})
        return {"content": content, "refs": refs}

    def chat_stream(self, system: str, messages: list[dict]) -> Iterator[str]:
        if self.ant:
            try:
                with self.ant.messages.stream(
                    model=settings.anthropic_model,
                    max_tokens=1500,
                    system=system,
                    messages=messages,
                ) as stream:
                    for text in stream.text_stream:
                        yield text
                return
            except Exception as e:
                print(f"[llm] anthropic stream error: {e}")
        if self.oai:
            try:
                oai_msgs = [{"role": "system", "content": system}] + messages
                stream = self.oai.chat.completions.create(
                    model=settings.openai_model,
                    messages=oai_msgs,
                    stream=True,
                )
                for chunk in stream:
                    delta = chunk.choices[0].delta.content
                    if delta:
                        yield delta
                return
            except Exception as e:
                print(f"[llm] openai stream error: {e}")
        yield "(LLM unavailable: no API key configured.)"

    # ---------- internal ----------

    def _json_call(self, system: str, user: str, max_tokens: int) -> dict:
        if self.ant:
            try:
                resp = self.ant.messages.create(
                    model=settings.anthropic_model,
                    max_tokens=max_tokens,
                    temperature=0.2,
                    system=system + "\nRespond with ONLY a single JSON object. No markdown.",
                    messages=[{"role": "user", "content": user}],
                )
                return _parse_json(resp.content[0].text)
            except Exception as e:
                print(f"[llm] anthropic json error: {e}")
        if self.oai:
            try:
                resp = self.oai.chat.completions.create(
                    model=settings.openai_model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    response_format={"type": "json_object"},
                    temperature=0.2,
                )
                return _parse_json(resp.choices[0].message.content or "")
            except Exception as e:
                print(f"[llm] openai json error: {e}")
        raise LLMUnavailable("no LLM backend available")


# ---------- helpers ----------

_SUMMARIZE_SYSTEM = (
    "You summarize git commits into structured JSON. Be terse and specific. "
    "For each commit, extract the architectural intent, the why, and impact. "
    "Return JSON: {\"results\":[{sha,title,why,impact,modules,tags,risk,confidence}]}. "
    "title<=80 chars, why<=200 chars, impact<=160 chars, risk<=80 chars, confidence 0..1."
)

_CLUSTER_SYSTEM = (
    "You cluster commit summaries into higher-level knowledge nodes describing the evolution "
    "of a codebase. Output JSON: {\"nodes\":[{id,kind,title,summary,member_shas}]}. "
    "kind in {theme, module, refactor, architecture}. Prefer 6-14 nodes. Each node's "
    "member_shas must reference real input shas."
)

_REPORT_SYSTEM = (
    "You write a concise section of a repository knowledge-transfer report. "
    "Output JSON: {\"content\":\"...\",\"refs\":[{\"sha\":\"...\"} or {\"branch\":\"...\"} or {\"file\":\"...\"}]}. "
    "Ground claims with inline [sha:HASH], [branch:NAME], [file:PATH] tags. Keep prose tight."
)


def _build_summarize_prompt(batch: list[dict]) -> str:
    lines: list[str] = ["Summarize each commit:"]
    for i, c in enumerate(batch, 1):
        msg = (c.get("message") or "").splitlines()[0][:160]
        files = ", ".join(f["path"] for f in c.get("files", [])[:8])
        stats = f"files={c.get('files_changed',0)} +{c.get('insertions',0)} -{c.get('deletions',0)}"
        typ = c.get("decision_type") or "?"
        diff = (c.get("diff") or "").strip()
        if diff:
            diff = diff[: settings.max_hunk_chars]
        lines.append(
            f"[{i}] sha={c['sha'][:12]} type={typ} {stats}\n  msg: {msg}\n  files: {files}"
            + (f"\n  diff: {diff}" if diff else "")
        )
    return "\n".join(lines)


_JSON_FENCE = re.compile(r"```(?:json)?\s*|\s*```", re.IGNORECASE)


def _parse_json(text: str) -> dict:
    if not text:
        return {}
    t = text.strip()
    t = _JSON_FENCE.sub("", t).strip()
    # Clip to the outermost braces if garbage surrounds the JSON.
    start = t.find("{")
    end = t.rfind("}")
    if start >= 0 and end > start:
        t = t[start : end + 1]
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        return {}


def _clip(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _as_list(v) -> list[str]:
    if isinstance(v, list):
        return [str(x)[:40] for x in v][:8]
    if isinstance(v, str) and v:
        return [v[:40]]
    return []
