"""
code_parser.py — Regex-based source code entity extractor.

Zero LLM calls. Extracts functions, classes, and import edges from source
files using language-specific regex patterns.
Supports: Python, JavaScript/TypeScript, Go, Java, Rust.
"""

import os
import re

_LANG_MAP: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".java": "java",
    ".rs": "rust",
    ".rb": "ruby",
    ".php": "php",
}


def detect_lang(path: str) -> str | None:
    ext = os.path.splitext(path)[1].lower()
    return _LANG_MAP.get(ext)


# ---- Python ---------------------------------------------------------------

def _py_docstring(body: str) -> str:
    s = body.lstrip()
    for q in ('"""', "'''"):
        if s.startswith(q):
            end = s.find(q, 3)
            return s[3:end].strip() if end > 3 else ""
    return ""


def _extract_python(content: str, path: str) -> list[dict]:
    entities: list[dict] = []
    for m in re.finditer(
        r'^([ \t]*)def\s+(\w+)\s*(\([^)]*(?:\)[^:]*)?)\s*:',
        content,
        re.MULTILINE,
    ):
        indent = len(m.group(1).expandtabs(4))
        name = m.group(2)
        sig = f"def {name}{m.group(3)}"
        line_no = content[: m.start()].count("\n") + 1
        body_start = content.find("\n", m.end()) + 1
        docstring = _py_docstring(content[body_start:])
        snippet = content[m.start() : m.start() + 600].split("\n")[:20]
        entities.append({
            "kind": "method" if indent > 0 else "function",
            "name": name,
            "path": path,
            "signature": sig[:120],
            "docstring": docstring[:300],
            "code_snippet": "\n".join(snippet)[:600],
            "line_start": line_no,
        })
    for m in re.finditer(r'^class\s+(\w+)\s*(?:\([^)]*\))?\s*:', content, re.MULTILINE):
        name = m.group(1)
        line_no = content[: m.start()].count("\n") + 1
        body_start = content.find("\n", m.end()) + 1
        docstring = _py_docstring(content[body_start:])
        snippet = content[m.start() : m.start() + 400].split("\n")[:10]
        entities.append({
            "kind": "class",
            "name": name,
            "path": path,
            "signature": f"class {name}",
            "docstring": docstring[:300],
            "code_snippet": "\n".join(snippet)[:400],
            "line_start": line_no,
        })
    return entities


def _py_imports(content: str) -> list[str]:
    imports: list[str] = []
    for m in re.finditer(
        r'^(?:from\s+([\w.]+)\s+import|import\s+([\w.,\s]+))', content, re.MULTILINE
    ):
        raw = m.group(1) or m.group(2) or ""
        for part in raw.split(","):
            mod = part.strip().split(".")[0]
            if mod:
                imports.append(mod)
    return imports


# ---- JavaScript / TypeScript -----------------------------------------------

def _extract_js(content: str, path: str) -> list[dict]:
    entities: list[dict] = []
    patterns = [
        (r'(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*(\([^)]*\))', "function"),
        (r'(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s+)?\([^)]*\)\s*=>', "function"),
        (r'(?:export\s+)?(?:default\s+)?class\s+(\w+)', "class"),
    ]
    seen: set[str] = set()
    for pat, kind in patterns:
        for m in re.finditer(pat, content, re.MULTILINE):
            name = m.group(1)
            if name in seen:
                continue
            seen.add(name)
            sig = m.group(0)[:120]
            line_no = content[: m.start()].count("\n") + 1
            snippet = content[m.start() : m.start() + 400].split("\n")[:10]
            entities.append({
                "kind": kind,
                "name": name,
                "path": path,
                "signature": sig,
                "docstring": "",
                "code_snippet": "\n".join(snippet)[:400],
                "line_start": line_no,
            })
    return entities


def _js_imports(content: str) -> list[str]:
    imports: list[str] = []
    for m in re.finditer(r"""(?:import|require)\s*(?:[^'"]*['"])([^'"]+)['"]""", content):
        imports.append(m.group(1))
    return imports


# ---- Go --------------------------------------------------------------------

def _extract_go(content: str, path: str) -> list[dict]:
    entities: list[dict] = []
    for m in re.finditer(
        r'^func\s+(?:\([^)]+\)\s+)?(\w+)\s*(\([^)]*\)(?:\s*(?:\([^)]*\)|\w[\w*.\[\]]*)?)?)',
        content,
        re.MULTILINE,
    ):
        name = m.group(1)
        sig = m.group(0)[:120]
        line_no = content[: m.start()].count("\n") + 1
        snippet = content[m.start() : m.start() + 400].split("\n")[:10]
        entities.append({
            "kind": "function",
            "name": name,
            "path": path,
            "signature": sig,
            "docstring": "",
            "code_snippet": "\n".join(snippet)[:400],
            "line_start": line_no,
        })
    return entities


# ---- Java ------------------------------------------------------------------

def _extract_java(content: str, path: str) -> list[dict]:
    entities: list[dict] = []
    method_pat = re.compile(
        r'(?:public|private|protected|static|final|synchronized|\s)+'
        r'(?:void|[A-Z]\w+|int|long|boolean|String|double|float|List|Map|Optional)'
        r'\s+(\w+)\s*\([^)]*\)\s*(?:throws\s+[\w,\s]+)?\s*\{',
        re.MULTILINE,
    )
    for m in method_pat.finditer(content):
        name = m.group(1)
        sig = m.group(0)[:120].rstrip("{").strip()
        line_no = content[: m.start()].count("\n") + 1
        snippet = content[m.start() : m.start() + 400].split("\n")[:10]
        entities.append({
            "kind": "function",
            "name": name,
            "path": path,
            "signature": sig,
            "docstring": "",
            "code_snippet": "\n".join(snippet)[:400],
            "line_start": line_no,
        })
    for m in re.finditer(r'(?:public|abstract)?\s*class\s+(\w+)', content, re.MULTILINE):
        name = m.group(1)
        line_no = content[: m.start()].count("\n") + 1
        snippet = content[m.start() : m.start() + 300].split("\n")[:8]
        entities.append({
            "kind": "class",
            "name": name,
            "path": path,
            "signature": f"class {name}",
            "docstring": "",
            "code_snippet": "\n".join(snippet)[:300],
            "line_start": line_no,
        })
    return entities


# ---- Rust ------------------------------------------------------------------

def _extract_rust(content: str, path: str) -> list[dict]:
    entities: list[dict] = []
    for m in re.finditer(
        r'^(?:pub\s+)?(?:async\s+)?fn\s+(\w+)\s*(?:<[^>]*>)?\s*(\([^)]*\))',
        content,
        re.MULTILINE,
    ):
        name = m.group(1)
        sig = m.group(0)[:120]
        line_no = content[: m.start()].count("\n") + 1
        snippet = content[m.start() : m.start() + 400].split("\n")[:10]
        entities.append({
            "kind": "function",
            "name": name,
            "path": path,
            "signature": sig,
            "docstring": "",
            "code_snippet": "\n".join(snippet)[:400],
            "line_start": line_no,
        })
    for m in re.finditer(r'^(?:pub\s+)?struct\s+(\w+)', content, re.MULTILINE):
        name = m.group(1)
        line_no = content[: m.start()].count("\n") + 1
        entities.append({
            "kind": "class",
            "name": name,
            "path": path,
            "signature": f"struct {name}",
            "docstring": "",
            "code_snippet": "",
            "line_start": line_no,
        })
    return entities


# ---- Public API ------------------------------------------------------------

def extract_entities(path: str, content: str) -> list[dict]:
    """Extract code entities from a source file. Returns list of entity dicts (no LLM)."""
    lang = detect_lang(path)
    if lang == "python":
        return _extract_python(content, path)
    if lang in ("javascript", "typescript"):
        return _extract_js(content, path)
    if lang == "go":
        return _extract_go(content, path)
    if lang == "java":
        return _extract_java(content, path)
    if lang == "rust":
        return _extract_rust(content, path)
    return []


def extract_imports(path: str, content: str) -> list[str]:
    """Return raw import module names from a source file (for building import edges)."""
    lang = detect_lang(path)
    if lang == "python":
        return _py_imports(content)
    if lang in ("javascript", "typescript"):
        return _js_imports(content)
    return []
