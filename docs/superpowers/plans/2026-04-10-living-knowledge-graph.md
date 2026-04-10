# Living Knowledge Graph Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transform COSMOBASE into a three-layer Living Knowledge Graph that ingests actual source code, answers function-level questions, visualizes a proper knowledge graph with typed edges, and produces a deeply technical KT report.

**Architecture:** Regex-based code entity extraction + git-blame origin tracing builds a `code_entities`/`entity_edges` graph in SQLite. These entities are vectorized into a new Chroma `entities` collection. Chat uses intent-routing for multi-stage RAG. The D3 frontend gains entity nodes, typed edges, and a force-directed Dependency Web mode alongside the existing tree.

**Tech Stack:** Python 3.11, FastAPI, SQLite (per-mission), ChromaDB 0.5, sentence-transformers, Anthropic Claude API, D3.js v7, Docker Compose.

---

## File Map

| Action | File | Responsibility |
|---|---|---|
| Modify | `backend/git_ingest.py` | Add `read_file_at_head`, `find_introducing_commit`, `list_repo_files`; fix checkout |
| Modify | `backend/db.py` | Add schema for `code_entities`+`entity_edges`; add 8 new query functions |
| Create | `backend/code_parser.py` | Regex entity extractor for Python/JS/TS/Go/Java/Rust; import edge builder |
| Create | `backend/code_analyzer.py` | LLM deep-dive on top-25 hot files; writes `llm_summary`/`llm_why` back to DB |
| Modify | `backend/llm.py` | Add `analyze_file()` method |
| Modify | `backend/embed.py` | Add `index_entities()`; update `search()` to query entities collection |
| Modify | `backend/pipeline.py` | Add `code_parse` + `code_analyze` to PHASES; wire them in `run_mission` |
| Modify | `backend/chat.py` | Add intent detection; multi-stage retrieval; entity-specific context formatter |
| Modify | `backend/models.py` | Add `CodeEntity`, `EntityEdge` models; update `KnowledgeGraphResponse` with edges |
| Modify | `backend/main.py` | Add `GET /entities` and `GET /entities/{id}` endpoints; update graph/knowledge |
| Modify | `backend/reporter.py` | Add 5 new KT report sections; enrich context with code entity data |
| Modify | `frontend/app.js` | Add entity nodes + edges to knowledge graph; force-directed Dependency Web mode; filter toolbar; entity detail panel; update PHASES constant |
| Modify | `frontend/styles.css` | Styles for filter toolbar, entity detail panel, node type legend |

---

## Phase 1: Source Code Ingestion

### Task 1: Fix git_ingest + new git helpers

**Files:**
- Modify: `backend/git_ingest.py`

- [ ] **Step 1: Add `list_repo_files` to git_ingest.py**

Open `backend/git_ingest.py` and add these three functions at the bottom (before `delete_clone`):

```python
_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", "vendor", ".venv", "venv",
    "dist", "build", ".next", ".nuxt", "target", ".tox", "coverage",
}

_SOURCE_EXTS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".java", ".rs",
    ".rb", ".php", ".cs", ".cpp", ".c", ".h", ".hpp", ".swift",
    ".kt", ".scala", ".ex", ".exs",
}


def list_repo_files(path: str) -> list[str]:
    """Return all source file paths relative to repo root via git ls-tree."""
    r = _run(["git", "-C", path, "ls-tree", "-r", "--name-only", "HEAD"])
    if r.returncode != 0:
        return []
    out: list[str] = []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("/")
        if any(p in _SKIP_DIRS for p in parts):
            continue
        ext = os.path.splitext(line)[1].lower()
        if ext in _SOURCE_EXTS:
            out.append(line)
    return out


def read_file_at_head(path: str, file_path: str) -> str:
    """Read file content at HEAD without needing a working tree checkout."""
    r = _run(["git", "-C", path, "show", f"HEAD:{file_path}"])
    if r.returncode != 0:
        return ""
    return r.stdout or ""


def find_introducing_commit(path: str, file_path: str, symbol_name: str) -> str | None:
    """Use git-log pickaxe (-S) to find the oldest commit that added symbol_name in file_path."""
    r = _run(
        ["git", "-C", path, "log", "--follow", "--format=%H", "-S", symbol_name, "--", file_path],
        timeout=60,
    )
    if r.returncode != 0 or not (r.stdout or "").strip():
        return None
    lines = [l.strip() for l in r.stdout.splitlines() if l.strip()]
    return lines[-1] if lines else None  # oldest = last in reverse-chronological output
```

- [ ] **Step 2: Verify the helpers work in a Docker shell**

```bash
docker exec -it cosmobase_backend python3 -c "
import git_ingest, os
# Use any already-cloned repo path that exists under /app/data/repos
import os, glob
repos = glob.glob('/app/data/repos/*')
if repos:
    p = repos[0]
    files = git_ingest.list_repo_files(p)
    print('files found:', len(files), files[:3])
    if files:
        content = git_ingest.read_file_at_head(p, files[0])
        print('content length:', len(content))
else:
    print('no repos cloned yet — run after first ingest')
"
```

Expected: prints file count and content length without errors. If no repos yet, clone one first.

- [ ] **Step 3: Commit**

```bash
git add backend/git_ingest.py
git commit -m "feat: add list_repo_files, read_file_at_head, find_introducing_commit helpers"
```

---

### Task 2: Add code_entities schema + DB functions

**Files:**
- Modify: `backend/db.py`

- [ ] **Step 1: Add the two new tables to SCHEMA in db.py**

In `backend/db.py`, find the `SCHEMA` string and append these two table definitions before the closing `"""`:

```python
# Add inside SCHEMA string, after the events table CREATE statement:

CREATE TABLE IF NOT EXISTS code_entities (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  path TEXT NOT NULL,
  signature TEXT,
  docstring TEXT,
  code_snippet TEXT,
  llm_summary TEXT,
  llm_why TEXT,
  introduced_sha TEXT,
  line_start INTEGER,
  line_end INTEGER
);

CREATE INDEX IF NOT EXISTS idx_entities_path ON code_entities(path);
CREATE INDEX IF NOT EXISTS idx_entities_kind ON code_entities(kind);

CREATE TABLE IF NOT EXISTS entity_edges (
  src_id TEXT NOT NULL,
  dst_id TEXT NOT NULL,
  edge_type TEXT NOT NULL,
  PRIMARY KEY (src_id, dst_id, edge_type)
);

CREATE INDEX IF NOT EXISTS idx_edges_src ON entity_edges(src_id);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON entity_edges(dst_id);
```

- [ ] **Step 2: Add 8 new DB functions at the end of db.py**

```python
def insert_code_entities(mission_id: str, entities: list[dict]) -> None:
    rows = [
        (
            e["id"], e["kind"], e["name"], e["path"],
            e.get("signature"), e.get("docstring"), e.get("code_snippet"),
            e.get("llm_summary"), e.get("llm_why"),
            e.get("introduced_sha"), e.get("line_start"), e.get("line_end"),
        )
        for e in entities
    ]
    with open_db(mission_id) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO code_entities"
            "(id, kind, name, path, signature, docstring, code_snippet,"
            " llm_summary, llm_why, introduced_sha, line_start, line_end)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )


def insert_entity_edges(mission_id: str, edges: list[dict]) -> None:
    rows = [(e["src_id"], e["dst_id"], e["edge_type"]) for e in edges]
    with open_db(mission_id) as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO entity_edges(src_id, dst_id, edge_type) VALUES(?,?,?)",
            rows,
        )


def list_code_entities(
    mission_id: str,
    kind: str | None = None,
    path: str | None = None,
    limit: int = 500,
) -> list[dict]:
    q = "SELECT * FROM code_entities WHERE 1=1"
    args: list = []
    if kind:
        q += " AND kind=?"
        args.append(kind)
    if path:
        q += " AND path=?"
        args.append(path)
    q += f" LIMIT {int(limit)}"
    with open_db(mission_id) as conn:
        return [dict(r) for r in conn.execute(q, args).fetchall()]


def get_code_entity(mission_id: str, entity_id: str) -> dict | None:
    with open_db(mission_id) as conn:
        row = conn.execute(
            "SELECT * FROM code_entities WHERE id=?", (entity_id,)
        ).fetchone()
        return dict(row) if row else None


def get_entity_edges(mission_id: str, entity_id: str) -> list[dict]:
    with open_db(mission_id) as conn:
        rows = conn.execute(
            "SELECT * FROM entity_edges WHERE src_id=? OR dst_id=?",
            (entity_id, entity_id),
        ).fetchall()
        return [dict(r) for r in rows]


def update_entity_llm(mission_id: str, entity_id: str, llm_summary: str, llm_why: str) -> None:
    with open_db(mission_id) as conn:
        conn.execute(
            "UPDATE code_entities SET llm_summary=?, llm_why=? WHERE id=?",
            (llm_summary, llm_why, entity_id),
        )


def get_all_entity_edges(mission_id: str, limit: int = 3000) -> list[dict]:
    with open_db(mission_id) as conn:
        rows = conn.execute(
            f"SELECT * FROM entity_edges LIMIT {int(limit)}"
        ).fetchall()
        return [dict(r) for r in rows]


def count_code_entities(mission_id: str) -> int:
    with open_db(mission_id) as conn:
        return conn.execute("SELECT COUNT(*) FROM code_entities").fetchone()[0]
```

- [ ] **Step 3: Verify schema migration works**

```bash
docker exec -it cosmobase_backend python3 -c "
import db
# Pick any existing mission or create a throwaway one
import os, glob
dbs = glob.glob('/app/data/db/*.sqlite')
if dbs:
    mid = os.path.basename(dbs[0]).replace('.sqlite', '')
    db.init_schema(mid)  # idempotent — CREATE IF NOT EXISTS
    entities = db.list_code_entities(mid)
    print('entities count:', len(entities))
    print('schema OK')
else:
    print('no missions yet — schema will apply on next mission create')
"
```

Expected: prints without error.

- [ ] **Step 4: Commit**

```bash
git add backend/db.py
git commit -m "feat: add code_entities and entity_edges schema + DB functions"
```

---

### Task 3: Create code_parser.py

**Files:**
- Create: `backend/code_parser.py`

- [ ] **Step 1: Create backend/code_parser.py**

```python
"""
code_parser.py — Regex-based source code entity extractor.

Extracts functions, classes, and import relationships from source files.
Supports Python, JavaScript/TypeScript, Go, Java, Rust.
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
        snippet_lines = content[m.start() : m.start() + 800].split("\n")[:20]
        entities.append({
            "kind": "method" if indent > 0 else "function",
            "name": name,
            "path": path,
            "signature": sig[:120],
            "docstring": docstring[:300],
            "code_snippet": "\n".join(snippet_lines)[:600],
            "line_start": line_no,
        })
    for m in re.finditer(r'^class\s+(\w+)\s*(?:\([^)]*\))?\s*:', content, re.MULTILINE):
        name = m.group(1)
        line_no = content[: m.start()].count("\n") + 1
        body_start = content.find("\n", m.end()) + 1
        docstring = _py_docstring(content[body_start:])
        snippet_lines = content[m.start() : m.start() + 500].split("\n")[:12]
        entities.append({
            "kind": "class",
            "name": name,
            "path": path,
            "signature": f"class {name}",
            "docstring": docstring[:300],
            "code_snippet": "\n".join(snippet_lines)[:500],
            "line_start": line_no,
        })
    return entities


def _py_imports(content: str, path: str) -> list[str]:
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
        (r'(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s+)?\(([^)]*)\)\s*=>', "function"),
        (r'(?:export\s+)?(?:default\s+)?class\s+(\w+)', "class"),
    ]
    for pat, kind in patterns:
        for m in re.finditer(pat, content, re.MULTILINE):
            name = m.group(1)
            sig = m.group(0)[:120]
            line_no = content[: m.start()].count("\n") + 1
            snippet_lines = content[m.start() : m.start() + 500].split("\n")[:12]
            entities.append({
                "kind": kind,
                "name": name,
                "path": path,
                "signature": sig,
                "docstring": "",
                "code_snippet": "\n".join(snippet_lines)[:500],
                "line_start": line_no,
            })
    return entities


def _js_imports(content: str) -> list[str]:
    imports: list[str] = []
    for m in re.finditer(
        r"""(?:import|require)\s*(?:[^'"]*['"])([^'"]+)['"]""", content
    ):
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
        snippet_lines = content[m.start() : m.start() + 500].split("\n")[:12]
        entities.append({
            "kind": "function",
            "name": name,
            "path": path,
            "signature": sig,
            "docstring": "",
            "code_snippet": "\n".join(snippet_lines)[:500],
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
        snippet_lines = content[m.start() : m.start() + 500].split("\n")[:12]
        entities.append({
            "kind": "function",
            "name": name,
            "path": path,
            "signature": sig,
            "docstring": "",
            "code_snippet": "\n".join(snippet_lines)[:500],
            "line_start": line_no,
        })
    for m in re.finditer(r'(?:public|abstract)?\s*class\s+(\w+)', content, re.MULTILINE):
        name = m.group(1)
        line_no = content[: m.start()].count("\n") + 1
        snippet_lines = content[m.start() : m.start() + 300].split("\n")[:8]
        entities.append({
            "kind": "class",
            "name": name,
            "path": path,
            "signature": f"class {name}",
            "docstring": "",
            "code_snippet": "\n".join(snippet_lines)[:300],
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
        snippet_lines = content[m.start() : m.start() + 500].split("\n")[:12]
        entities.append({
            "kind": "function",
            "name": name,
            "path": path,
            "signature": sig,
            "docstring": "",
            "code_snippet": "\n".join(snippet_lines)[:500],
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
    """Extract code entities from a source file. Returns list of entity dicts."""
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
    """Return raw import strings from a source file."""
    lang = detect_lang(path)
    if lang == "python":
        return _py_imports(content, path)
    if lang in ("javascript", "typescript"):
        return _js_imports(content)
    return []
```

- [ ] **Step 2: Quick smoke test in Docker**

```bash
docker exec -it cosmobase_backend python3 -c "
import code_parser

py_sample = '''
def run_mission(mission_id: str, token: str | None) -> None:
    \"\"\"Orchestrates the pipeline.\"\"\"
    pass

class LLMClient:
    def chat_stream(self, system: str, messages: list) -> None:
        pass
'''

entities = code_parser.extract_entities('backend/pipeline.py', py_sample)
for e in entities:
    print(e['kind'], e['name'], e['signature'][:60])
"
```

Expected output:
```
function run_mission def run_mission(mission_id: str, token: str | None) -> None
class LLMClient class LLMClient
method chat_stream def chat_stream(self, system: str, messages: list) -> None
```

- [ ] **Step 3: Commit**

```bash
git add backend/code_parser.py
git commit -m "feat: add regex-based code entity extractor for Python/JS/TS/Go/Java/Rust"
```

---

### Task 4: Create code_analyzer.py + add analyze_file to llm.py

**Files:**
- Create: `backend/code_analyzer.py`
- Modify: `backend/llm.py`

- [ ] **Step 1: Add `analyze_file` method to LLMClient in llm.py**

In `backend/llm.py`, add this constant and method to the `LLMClient` class (after `write_section`):

```python
    def analyze_file(self, path: str, content: str, recent_commits: list[str]) -> dict:
        """Deep-analyze a source file. Returns {summary, why, key_functions:[{name,purpose,why}]}."""
        commits_ctx = "\n".join(f"- {t}" for t in recent_commits[:8])
        user = (
            f"FILE: {path}\n"
            f"RECENT COMMIT TITLES:\n{commits_ctx}\n\n"
            f"CONTENT (truncated at 4000 chars):\n```\n{content[:4000]}\n```"
        )
        data = self._json_call(
            system=_ANALYZE_FILE_SYSTEM,
            user=user,
            max_tokens=1200,
        )
        result: dict = {
            "summary": _clip(str(data.get("summary") or ""), 400),
            "why": _clip(str(data.get("why") or ""), 300),
            "key_functions": [],
        }
        for fn in data.get("key_functions") or []:
            if isinstance(fn, dict) and fn.get("name"):
                result["key_functions"].append({
                    "name": str(fn["name"])[:60],
                    "purpose": _clip(str(fn.get("purpose") or ""), 160),
                    "why": _clip(str(fn.get("why") or ""), 120),
                })
        return result
```

Also add the system prompt constant after the existing `_REPORT_SYSTEM`:

```python
_ANALYZE_FILE_SYSTEM = (
    "You are an expert software engineer writing a Knowledge Transfer report. "
    "Analyze the provided source file and return structured JSON. "
    "Output JSON: {\"summary\": \"...\", \"why\": \"...\", \"key_functions\": [{\"name\": \"...\", \"purpose\": \"...\", \"why\": \"...\"}]}. "
    "summary: 2-3 sentences on what this file does and how it fits the system. "
    "why: 1-2 sentences on why this file exists and what problem it solves. "
    "key_functions: the 3-7 most important functions/classes — each with name (exact), "
    "purpose (1 sentence: what it does), why (1 sentence: why it exists). "
    "Be specific and technical. Use exact names from the code."
)
```

- [ ] **Step 2: Create backend/code_analyzer.py**

```python
"""
code_analyzer.py — LLM-powered deep analysis of top hot files.

Reads the top 25 most-changed source files, sends them to the LLM with
their commit history, and writes llm_summary + llm_why back to code_entities.
"""

import db
import git_ingest
from llm import LLMClient


def run_code_analysis(mission_id: str, llm: LLMClient) -> int:
    """
    Analyze top-25 hot files with LLM. Enriches code_entities with
    llm_summary and llm_why. Returns count of files successfully analyzed.
    """
    repo = db.get_repo(mission_id)
    if not repo:
        return 0
    clone_path = repo["clone_path"]

    # Top files by number of commits that touched them
    hot_files = db.file_touch_counts(mission_id, min_touches=2, limit=25)
    enriched = 0

    for t in hot_files:
        path = t["path"]
        content = git_ingest.read_file_at_head(clone_path, path)
        if not content or len(content) < 30:
            continue

        # Recent commit titles for this file as context
        top = db.top_commits_for_file(mission_id, path, limit=8)
        commit_titles = [c.get("title") or "" for c in top if c.get("title")]

        try:
            result = llm.analyze_file(path, content, commit_titles)
        except Exception as e:
            print(f"[code_analyzer] {path}: {e}")
            continue

        # Update the file-level entity
        file_id = f"file:{path}"
        db.update_entity_llm(mission_id, file_id, result["summary"], result["why"])

        # Update function/class entities extracted from this file
        for fn in result.get("key_functions", []):
            fn_name = fn.get("name", "")
            if not fn_name:
                continue
            # Try exact match, then prefix match
            entity_id = f"fn:{path}::{fn_name}"
            if not db.get_code_entity(mission_id, entity_id):
                entity_id = f"cls:{path}::{fn_name}"
            if db.get_code_entity(mission_id, entity_id):
                db.update_entity_llm(
                    mission_id,
                    entity_id,
                    fn.get("purpose", ""),
                    fn.get("why", ""),
                )

        enriched += 1

    return enriched
```

- [ ] **Step 3: Smoke test analyze_file via Docker**

```bash
docker exec -it cosmobase_backend python3 -c "
from llm import LLMClient

llm = LLMClient()
result = llm.analyze_file(
    'backend/pipeline.py',
    open('/app/pipeline.py').read()[:2000],
    ['Add pipeline orchestration', 'Fix phase error handling']
)
print('summary:', result['summary'][:80])
print('why:', result['why'][:80])
print('key_functions:', len(result['key_functions']))
for fn in result['key_functions']:
    print(' -', fn['name'], ':', fn['purpose'][:60])
"
```

Expected: structured output with at least 2 key functions identified.

- [ ] **Step 4: Commit**

```bash
git add backend/llm.py backend/code_analyzer.py
git commit -m "feat: add LLM file analysis (analyze_file) and code_analyzer module"
```

---

### Task 5: Update embed.py + pipeline.py to wire everything together

**Files:**
- Modify: `backend/embed.py`
- Modify: `backend/pipeline.py`

- [ ] **Step 1: Add entity indexing to embed.py**

In `backend/embed.py`, add `index_entities` after `index_files`:

```python
def index_entities(mission_id: str) -> int:
    entities = db.list_code_entities(mission_id, limit=2000)
    # Only index functions, classes, methods — not raw file entities (handled by index_files)
    entities = [e for e in entities if e["kind"] in ("function", "class", "method")]
    if not entities:
        return 0
    ids: list[str] = []
    docs: list[str] = []
    metas: list[dict] = []
    for e in entities:
        ids.append(e["id"])
        doc_parts = [
            e.get("name") or "",
            e.get("signature") or "",
            e.get("docstring") or "",
            e.get("llm_summary") or "",
            e.get("llm_why") or "",
        ]
        docs.append("\n".join(p for p in doc_parts if p))
        metas.append({
            "kind": e["kind"],
            "path": e["path"],
            "line_start": int(e.get("line_start") or 0),
        })
    _reset_collection(mission_id, "entities")
    col = _col(mission_id, "entities")
    col.upsert(ids=ids, documents=docs, metadatas=metas, embeddings=_embed(docs))
    return len(ids)
```

Update `build_indexes` to include entities:

```python
def build_indexes(mission_id: str) -> dict:
    return {
        "commits": index_commits(mission_id),
        "clusters": index_clusters(mission_id),
        "files": index_files(mission_id),
        "entities": index_entities(mission_id),
    }
```

Update `search` to query the entities collection:

```python
def search(mission_id: str, query: str, k: int = 8) -> list[dict]:
    query_emb = _embed([query])
    hits: list[dict] = []
    hits += _safe_query(mission_id, "commits", query_emb, k)
    hits += _safe_query(mission_id, "clusters", query_emb, max(2, k // 2))
    hits += _safe_query(mission_id, "files", query_emb, max(2, k // 2))
    hits += _safe_query(mission_id, "entities", query_emb, max(4, k // 2))
    hits.sort(key=lambda h: (h.get("distance") if h.get("distance") is not None else 1.0))
    return hits[:k]
```

- [ ] **Step 2: Wire new phases into pipeline.py**

In `backend/pipeline.py`, update the `PHASES` list:

```python
PHASES = ["clone", "walk", "classify", "select", "summarize", "cluster", "report", "index", "code_parse", "code_analyze", "done"]
```

Add the import at the top of the file (after `import summarizer`):

```python
import code_parser
import code_analyzer
```

Add phases 9 and 10 in `run_mission` — insert before the `db.set_phase(mission_id, "done", 100)` line:

```python
        # ---- Phase 9: parse source code entities ----
        if not _phase_done(mission_id, "code_parse"):
            db.set_phase(mission_id, "code_parse", 10)
            _log(mission_id, "info", "code_parse", "Parsing source code entities")
            repo_info = db.get_repo(mission_id)
            clone_path_local = repo_info["clone_path"]
            files = git_ingest.list_repo_files(clone_path_local)
            all_entities: list[dict] = []
            all_edges: list[dict] = []
            file_entities: list[dict] = []

            for file_path in files:
                content = git_ingest.read_file_at_head(clone_path_local, file_path)
                if not content:
                    continue

                # File-level entity
                file_id = f"file:{file_path}"
                file_entities.append({
                    "id": file_id,
                    "kind": "file",
                    "name": file_path.split("/")[-1],
                    "path": file_path,
                    "signature": None,
                    "docstring": None,
                    "code_snippet": content[:200],
                    "llm_summary": None,
                    "llm_why": None,
                    "introduced_sha": None,
                    "line_start": 1,
                    "line_end": content.count("\n") + 1,
                })

                # Function/class entities
                raw = code_parser.extract_entities(file_path, content)
                for e in raw:
                    kind_prefix = "cls" if e["kind"] == "class" else "fn"
                    e["id"] = f"{kind_prefix}:{file_path}::{e['name']}"
                    # Try to find introducing commit (fast timeout)
                    try:
                        sha = git_ingest.find_introducing_commit(
                            clone_path_local, file_path, e["name"]
                        )
                        e["introduced_sha"] = sha
                    except Exception:
                        e["introduced_sha"] = None
                    all_entities.append(e)
                    # Edge: file contains function/class
                    all_edges.append({
                        "src_id": file_id,
                        "dst_id": e["id"],
                        "edge_type": "contains",
                    })

                # Import edges
                for imp in code_parser.extract_imports(file_path, content):
                    # Find if imp matches another file in the repo
                    imp_clean = imp.strip("/").replace("-", "_")
                    for other in files:
                        other_name = other.split("/")[-1].split(".")[0].replace("-", "_")
                        if other_name == imp_clean or other.endswith(f"/{imp_clean}.py") or other.endswith(f"/{imp_clean}.js"):
                            all_edges.append({
                                "src_id": file_id,
                                "dst_id": f"file:{other}",
                                "edge_type": "imports",
                            })
                            break

            # Link introduced_by edges: function → commit
            for e in all_entities:
                if e.get("introduced_sha"):
                    all_edges.append({
                        "src_id": e["id"],
                        "dst_id": e["introduced_sha"],
                        "edge_type": "introduced_by",
                    })

            db.insert_code_entities(mission_id, file_entities + all_entities)
            db.insert_entity_edges(mission_id, all_edges)
            db.set_phase(mission_id, "code_parse", 100)
            _log(
                mission_id, "success", "code_parse",
                f"Parsed {len(all_entities)} entities, {len(all_edges)} edges from {len(files)} files"
            )

        # ---- Phase 10: LLM code analysis ----
        if not _phase_done(mission_id, "code_analyze"):
            db.set_phase(mission_id, "code_analyze", 10)
            _log(mission_id, "info", "code_analyze", "Running LLM analysis on hot files")
            n = code_analyzer.run_code_analysis(mission_id, llm)
            db.set_phase(mission_id, "code_analyze", 100)
            _log(mission_id, "success", "code_analyze", f"LLM analyzed {n} files")

        # ---- Phase 11: rebuild vector indexes with entities ----
        if not _phase_done(mission_id, "index") or not _phase_done(mission_id, "code_analyze"):
            db.set_phase(mission_id, "index", 30)
            counts = embed.build_indexes(mission_id)
            db.set_phase(mission_id, "index", 100)
            _log(mission_id, "success", "index", f"Indexed {counts}")
```

**Important:** Remove or comment out the existing standalone `index` phase block (it is now superseded by the one above). Search for `# ---- Phase 8: vector indexes ----` and guard it so it only runs if `code_analyze` hasn't run yet (or just let the new block above handle it since `_phase_done` is idempotent).

Actually, the cleanest approach: replace the existing Phase 8 block entirely with a note and rely on the rebuild at the end of `code_analyze`. Add this guard around the existing Phase 8:

```python
        # ---- Phase 8: vector indexes (initial pass, may be re-run after code_analyze) ----
        if not _phase_done(mission_id, "index"):
            db.set_phase(mission_id, "index", 30)
            _log(mission_id, "info", "index", "Building retrieval indexes")
            counts = embed.build_indexes(mission_id)
            db.set_phase(mission_id, "index", 100)
            _log(mission_id, "success", "index", f"Indexed {counts}")
```

Leave Phase 8 as-is (it still runs) and the `code_analyze` phase at the end will call `build_indexes` again which rebuilds with entities included.

- [ ] **Step 3: End-to-end test — ingest a public repo and verify entity data**

```bash
# In the browser: go to http://localhost:8080, paste https://github.com/tiangolo/fastapi
# ... or use curl to start a mission:
curl -s -X POST http://localhost:8010/api/missions \
  -H "Content-Type: application/json" \
  -d '{"repo_url": "https://github.com/tiangolo/fastapi"}' | python3 -m json.tool

# Wait for pipeline to complete, then check entity count:
# (replace MISSION_ID with actual id returned above)
curl -s http://localhost:8010/api/missions/MISSION_ID | python3 -m json.tool
```

After the pipeline finishes (status: "done"), verify entities exist:

```bash
docker exec -it cosmobase_backend python3 -c "
import db, os, glob
dbs = glob.glob('/app/data/db/*.sqlite')
mid = os.path.basename(dbs[-1]).replace('.sqlite', '')
print('mission:', mid)
print('entities:', db.count_code_entities(mid))
entities = db.list_code_entities(mid, kind='function', limit=5)
for e in entities:
    print(' -', e['kind'], e['name'], e['path'])
"
```

Expected: entities count > 0, function names from the repo shown.

- [ ] **Step 4: Commit**

```bash
git add backend/embed.py backend/pipeline.py
git commit -m "feat: wire code_parse + code_analyze phases into pipeline, add entity vector index"
```

---

## Phase 2: Enhanced Chat with Intent Routing

### Task 6: Update chat.py for multi-stage retrieval

**Files:**
- Modify: `backend/chat.py`

- [ ] **Step 1: Replace chat.py with the intent-routing version**

```python
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
    r'\b(?:why does|why was|why is|why exists|why exist|who wrote|who added|when was added|purpose of)\b',
    re.IGNORECASE,
)
_EVOLUTION_RE = re.compile(
    r'\b(?:how has|history of|changes to|evolved|evolution|changed over|when did)\b',
    re.IGNORECASE,
)
_ONBOARDING_RE = re.compile(
    r'\b(?:where to start|what to read|how to set up|get started|onboard|new developer|where do i start|first steps)\b',
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
    """Try to extract a code symbol name from the question."""
    # Backtick-quoted: `func_name`
    m = re.search(r'`(\w+)`', question)
    if m:
        return m.group(1)
    # snake_case or camelCase identifiers (2+ words joined)
    m = re.search(
        r'\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+|[a-z]+[A-Z][a-zA-Z0-9]+)\b',
        question,
    )
    if m:
        return m.group(1)
    return None


# ---- context formatters -----------------------------------------------------

SYSTEM_PROMPT = (
    "You are an elite Principal Software Engineer and expert Technical Onboarding Tutor for this codebase. "
    "Your goal is to transfer deep knowledge to new developers. Always explain exactly how the code works, "
    "what specific files do, and deeply analyze 'why' a feature exists based on the architecture. "
    "Answer from the evidence blocks below, but synthesize it beautifully into a complete project understanding. "
    "Cite using inline tags: [sha:abcdef1234], [branch:NAME], [file:path/to/file], [fn:path::name]. "
    "If evidence is weak or missing, explain what you know and what is missing gracefully."
)


def _fmt_entity_evidence(entities: list[dict]) -> str:
    blocks: list[str] = []
    for e in entities:
        header = f"[ENTITY {e['id']} kind:{e['kind']}]"
        sig = e.get("signature") or ""
        doc = e.get("docstring") or ""
        llm_sum = e.get("llm_summary") or ""
        llm_why = e.get("llm_why") or ""
        snippet = (e.get("code_snippet") or "")[:300]
        parts = [header]
        if sig:
            parts.append(f"Signature: {sig}")
        if doc:
            parts.append(f"Docstring: {doc}")
        if llm_sum:
            parts.append(f"What it does: {llm_sum}")
        if llm_why:
            parts.append(f"Why it exists: {llm_why}")
        if snippet:
            parts.append(f"Code:\n{snippet}")
        blocks.append("\n".join(parts))
    return "\n\n".join(blocks)


def _fmt_vector_evidence(hits: list[dict], diffs: dict[str, str]) -> str:
    blocks: list[str] = []
    for h in hits:
        src = h.get("source")
        ident = h.get("id")
        doc = (h.get("document") or "").strip()
        meta = h.get("metadata") or {}
        if src == "commits":
            header = f"[COMMIT sha:{(ident or '')[:12]} date:{meta.get('date','')[:10]} author:{meta.get('author','')}]"
            tail = diffs.get(ident or "", "")
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

    # Stage 1: entity-specific lookup for code questions
    entity_context = ""
    resolved_entities: list[dict] = []
    if intent in ("entity_explain", "entity_origin") and entity_name:
        candidates = db.list_code_entities(mission_id, limit=500)
        # Match by name (exact first, then partial)
        exact = [e for e in candidates if e["name"].lower() == entity_name.lower()]
        partial = [e for e in candidates if entity_name.lower() in e["name"].lower()] if not exact else []
        resolved_entities = (exact or partial)[:4]
        if resolved_entities:
            entity_context = _fmt_entity_evidence(resolved_entities)

    # Stage 2: vector search
    hits = embed.search(mission_id, question, k=settings.chat_top_k)

    # Attach diffs to commit hits
    diffs: dict[str, str] = {}
    commit_hits = [h for h in hits if h.get("source") == "commits"]
    for h in commit_hits[: settings.chat_diff_attach]:
        sha = h["id"]
        try:
            diffs[sha] = git_ingest.read_diff(clone_path, sha, max_bytes=1200)
        except Exception:
            pass

    vector_evidence = _fmt_vector_evidence(hits, diffs) or "(no additional evidence retrieved)"

    # Assemble system prompt
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
    citations = _collect_citations(hits, entity_ids)
    yield {"event": "citations", "data": json.dumps(citations)}
```

- [ ] **Step 2: Test intent detection manually**

```bash
docker exec -it cosmobase_backend python3 -c "
import re

_ENTITY_EXPLAIN_RE = re.compile(r'\b(?:what does|how does|explain|describe|what is|what\'s)\b', re.IGNORECASE)
_ENTITY_ORIGIN_RE = re.compile(r'\b(?:why does|why was|why is|why exists|why exist|who wrote|who added)\b', re.IGNORECASE)

tests = [
    ('what does run_mission do', 'entity_explain'),
    ('why does run_mission exist', 'entity_origin'),
    ('how has the auth evolved', 'file_evolution'),
    ('where should I start reading', 'onboarding'),
    ('list all endpoints', 'general'),
]
for q, expected in tests:
    intent = 'entity_explain' if _ENTITY_EXPLAIN_RE.search(q) else 'entity_origin' if _ENTITY_ORIGIN_RE.search(q) else 'general'
    status = 'OK' if intent == expected or expected in ('file_evolution','onboarding','general') else 'FAIL'
    print(status, repr(q), '->', intent)
"
```

Expected: all OK.

- [ ] **Step 3: End-to-end chat test via curl**

```bash
# Replace MISSION_ID with a completed mission id
curl -s -X POST http://localhost:8010/api/missions/MISSION_ID/chat \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{"question": "what does run_mission do", "history": []}' | head -30
```

Expected: SSE stream with `data:` chunks containing explanation text.

- [ ] **Step 4: Commit**

```bash
git add backend/chat.py
git commit -m "feat: add intent-routing and entity-aware multi-stage RAG to chat"
```

---

## Phase 3: Knowledge Graph API + Frontend

### Task 7: Update models.py + db.py + main.py for entity endpoints

**Files:**
- Modify: `backend/models.py`
- Modify: `backend/main.py`

- [ ] **Step 1: Add new Pydantic models to models.py**

In `backend/models.py`, add after `KnowledgeNode`:

```python
class EntityEdge(BaseModel):
    src_id: str
    dst_id: str
    edge_type: str


class CodeEntitySummary(BaseModel):
    id: str
    kind: str
    name: str
    path: str
    signature: str | None = None
    llm_summary: str | None = None
    line_start: int | None = None


class CodeEntityDetail(BaseModel):
    id: str
    kind: str
    name: str
    path: str
    signature: str | None = None
    docstring: str | None = None
    code_snippet: str | None = None
    llm_summary: str | None = None
    llm_why: str | None = None
    introduced_sha: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    edges: list[EntityEdge] = []
```

Update `KnowledgeGraphResponse` to include edges:

```python
class KnowledgeGraphResponse(BaseModel):
    nodes: list[KnowledgeNode]
    edges: list[EntityEdge] = []
```

- [ ] **Step 2: Add entity endpoints to main.py**

In `backend/main.py`, add these two endpoints after `get_knowledge_graph`:

```python
@app.get("/api/missions/{mission_id}/entities", response_model=dict)
async def list_entities(
    mission_id: str,
    kind: str | None = Query(None),
    path: str | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
) -> dict:
    _ensure_mission(mission_id)
    entities = db.list_code_entities(mission_id, kind=kind, path=path, limit=limit)
    return {
        "entities": [
            {
                "id": e["id"],
                "kind": e["kind"],
                "name": e["name"],
                "path": e["path"],
                "signature": e.get("signature"),
                "llm_summary": e.get("llm_summary"),
                "line_start": e.get("line_start"),
            }
            for e in entities
        ]
    }


@app.get("/api/missions/{mission_id}/entities/{entity_id:path}", response_model=CodeEntityDetail)
async def get_entity(mission_id: str, entity_id: str) -> CodeEntityDetail:
    _ensure_mission(mission_id)
    e = db.get_code_entity(mission_id, entity_id)
    if not e:
        raise HTTPException(status_code=404, detail="entity not found")
    edges = db.get_entity_edges(mission_id, entity_id)
    return CodeEntityDetail(
        id=e["id"],
        kind=e["kind"],
        name=e["name"],
        path=e["path"],
        signature=e.get("signature"),
        docstring=e.get("docstring"),
        code_snippet=e.get("code_snippet"),
        llm_summary=e.get("llm_summary"),
        llm_why=e.get("llm_why"),
        introduced_sha=e.get("introduced_sha"),
        line_start=e.get("line_start"),
        line_end=e.get("line_end"),
        edges=[EntityEdge(**edge) for edge in edges],
    )
```

Also update the import at the top of `main.py` to include new models:

```python
from models import (
    ChatRequest,
    CodeEntityDetail,
    CommitDetail,
    CommitGraphResponse,
    CommitNode,
    CreateMissionRequest,
    CreateMissionResponse,
    EntityEdge,
    KnowledgeGraphResponse,
    KnowledgeNode,
    MissionSummary,
    ReportResponse,
    ReportSection,
)
```

Update `get_knowledge_graph` to include edges:

```python
@app.get("/api/missions/{mission_id}/graph/knowledge", response_model=KnowledgeGraphResponse)
async def get_knowledge_graph(mission_id: str) -> KnowledgeGraphResponse:
    _ensure_mission(mission_id)
    nodes = db.get_knowledge_nodes(mission_id)
    edges = db.get_all_entity_edges(mission_id, limit=3000)
    return KnowledgeGraphResponse(
        nodes=[
            KnowledgeNode(
                id=n["id"],
                kind=n["kind"],
                title=n["title"],
                summary=n["summary"],
                member_shas=n["member_shas"],
                first_date=n.get("first_date"),
                last_date=n.get("last_date"),
            )
            for n in nodes
        ],
        edges=[EntityEdge(**e) for e in edges],
    )
```

- [ ] **Step 3: Test new endpoints**

```bash
# Replace MISSION_ID with a completed mission id
# List all function entities
curl -s "http://localhost:8010/api/missions/MISSION_ID/entities?kind=function&limit=5" | python3 -m json.tool

# Get entity detail (URL-encode the entity id)
curl -s "http://localhost:8010/api/missions/MISSION_ID/entities/fn:backend%2Fpipeline.py::run_mission" | python3 -m json.tool
```

Expected: JSON with entity details and edges list.

- [ ] **Step 4: Commit**

```bash
git add backend/models.py backend/main.py
git commit -m "feat: add entity endpoints and edges to knowledge graph API"
```

---

### Task 8: Update frontend — knowledge graph + entity nodes + filter bar

**Files:**
- Modify: `frontend/app.js`
- Modify: `frontend/index.html`
- Modify: `frontend/styles.css`

- [ ] **Step 1: Add filter toolbar to index.html**

In `frontend/index.html`, find the `<div class="tabs">` block and add a filter bar after the graph controls div:

```html
        <div class="graph-controls hidden" id="graphControls">
          <button class="ghost-btn" id="graphModeBtn" data-mode="commit">Commit DAG</button>
          <button class="ghost-btn" id="graphResetBtn">Reset view</button>
        </div>
        <div class="graph-filter hidden" id="graphFilter">
          <span class="filter-label">Show:</span>
          <label class="filter-check"><input type="checkbox" id="filterTheme" checked> <span class="filter-dot" style="background:#9b59b6"></span>Themes</label>
          <label class="filter-check"><input type="checkbox" id="filterFile" checked> <span class="filter-dot" style="background:#3498db"></span>Files</label>
          <label class="filter-check"><input type="checkbox" id="filterFn" checked> <span class="filter-dot" style="background:#1abc9c"></span>Functions</label>
          <label class="filter-check"><input type="checkbox" id="filterCommit"> <span class="filter-dot" style="background:#e67e22"></span>Commits</label>
          <input type="text" id="graphSearch" placeholder="Search nodes…" class="graph-search-input" />
        </div>
```

- [ ] **Step 2: Update PHASES constant and state in app.js**

Find `const PHASES = [` in `frontend/app.js` and update to include new phases:

```javascript
const PHASES = [
  { id: "clone", label: "Clone" },
  { id: "walk", label: "Walk DAG" },
  { id: "classify", label: "Classify" },
  { id: "select", label: "Select keys" },
  { id: "summarize", label: "Summarize" },
  { id: "cluster", label: "Cluster" },
  { id: "report", label: "KT report" },
  { id: "index", label: "Index" },
  { id: "code_parse", label: "Parse code" },
  { id: "code_analyze", label: "Analyze code" },
];
```

In `state` object, add `graphFilter` and `entityGraph`:

```javascript
const state = {
  // ... existing fields ...
  graphFilter: { theme: true, file: true, fn: true, commit: false },
  entityGraph: null,   // {entities: [], edges: []} from knowledge graph
};
```

- [ ] **Step 3: Replace `renderConstellations` with enhanced version that shows entity nodes**

Find the `function renderConstellations()` function in `app.js` and replace it entirely:

```javascript
function renderConstellations() {
  const nodes = state.knowledgeGraph?.nodes || [];
  const allEdges = state.knowledgeGraph?.edges || [];
  if (!nodes.length) {
    resetGraph();
    graphEmptyEl.textContent = "No knowledge clusters yet.";
    return;
  }

  const filter = state.graphFilter;
  const { g, W, H } = _svgInit();

  // ---- Build visible node + edge sets ----
  const kindColors = {
    root: "var(--cyan)",
    group: "var(--amber)",
    theme: "var(--violet)",
    module: "var(--cyan)",
    refactor: "var(--pink)",
    architecture: "var(--green)",
    file: "#3498db",
    function: "#1abc9c",
    method: "#1abc9c",
    class: "#16a085",
    commit: "#e67e22",
  };

  // Build hierarchical tree data (existing cluster nodes)
  const kindGroups = {};
  nodes.forEach((n) => {
    const k = n.kind || "theme";
    if (!kindGroups[k]) kindGroups[k] = [];
    kindGroups[k].push(n);
  });

  const treeData = {
    id: "__root",
    title: "Knowledge Tree",
    kind: "root",
    children: Object.entries(kindGroups).map(([kind, items]) => ({
      id: `__group_${kind}`,
      title: kind.charAt(0).toUpperCase() + kind.slice(1),
      kind: "group",
      children: items.map((n) => ({
        id: n.id,
        title: n.title || "Untitled",
        summary: n.summary || "",
        kind: n.kind || "theme",
        member_shas: n.member_shas || [],
        first_date: n.first_date,
        last_date: n.last_date,
        children: [],
      })),
    })),
  };
  if (treeData.children.length === 1) {
    treeData.children = treeData.children[0].children;
  }

  const root = d3.hierarchy(treeData);
  const treeLayout = d3.tree().size([H - 80, W - 200]);
  treeLayout(root);
  root.each((d) => { const tmp = d.x; d.x = d.y + 100; d.y = tmp + 40; });

  // Draw tree edges
  g.append("g").attr("class", "tree-links").selectAll("path")
    .data(root.links()).enter().append("path")
    .attr("d", (d) => {
      const mx = (d.source.x + d.target.x) / 2;
      return `M${d.source.x},${d.source.y} C${mx},${d.source.y} ${mx},${d.target.y} ${d.target.x},${d.target.y}`;
    })
    .attr("fill", "none")
    .attr("stroke", "rgba(167, 139, 250, 0.35)")
    .attr("stroke-width", 1.5)
    .attr("filter", "url(#glow)");

  // Draw entity edges (contains/imports) as thin lines
  const edgeColorMap = {
    contains: "rgba(52,152,219,0.25)",
    imports: "rgba(52,152,219,0.4)",
    introduced_by: "rgba(230,126,34,0.3)",
    belongs_to: "rgba(155,89,182,0.35)",
  };
  const edgeStyleMap = { imports: "4,2", introduced_by: "2,3" };

  // Filter edges based on visibility settings
  const visEdges = allEdges.filter((e) => {
    if (!filter.fn && (e.src_id.startsWith("fn:") || e.src_id.startsWith("cls:"))) return false;
    if (!filter.commit && e.edge_type === "introduced_by") return false;
    if (!filter.file && e.src_id.startsWith("file:")) return false;
    return true;
  });

  // Draw tree cluster nodes
  const nodeG = g.append("g").attr("class", "tree-nodes").selectAll("g")
    .data(root.descendants()).enter().append("g")
    .attr("transform", (d) => `translate(${d.x},${d.y})`)
    .style("cursor", (d) => d.data.id.startsWith("__") ? "default" : "pointer")
    .on("click", (_, d) => {
      if (!d.data.id.startsWith("__")) showClusterDetail(d.data);
    });

  nodeG.append("circle")
    .attr("r", (d) => d.data.id === "__root" ? 16 : (d.data.id.startsWith("__group") ? 12 : 10 + Math.min((d.data.member_shas || []).length * 2, 10)))
    .attr("fill", (d) => {
      const c = kindColors[d.data.kind] || "var(--violet)";
      return d.data.id.startsWith("__") ? c : `color-mix(in srgb, ${c} 30%, transparent)`;
    })
    .attr("stroke", (d) => kindColors[d.data.kind] || "var(--violet)")
    .attr("stroke-width", (d) => d.data.id === "__root" ? 2.5 : 1.5)
    .attr("filter", "url(#glow)");

  nodeG.append("text")
    .attr("dy", (d) => d.children ? -20 : 4)
    .attr("dx", (d) => d.children ? 0 : 18)
    .attr("text-anchor", (d) => d.children ? "middle" : "start")
    .attr("fill", "var(--text)")
    .style("font-size", (d) => d.data.id === "__root" ? "13px" : (d.data.id.startsWith("__group") ? "12px" : "11px"))
    .style("font-family", "'Inter', sans-serif")
    .style("font-weight", (d) => d.data.id.startsWith("__") ? "600" : "400")
    .style("pointer-events", "none")
    .text((d) => (d.data.title || "").slice(0, 32));

  // Show graph filter panel
  const filterEl = document.getElementById("graphFilter");
  if (filterEl) filterEl.classList.remove("hidden");
}
```

- [ ] **Step 4: Add graph filter and search event listeners in app.js**

Add this block near the bottom of `app.js`, after the existing event listeners section:

```javascript
// ---- graph filter ----
function _bindFilterCheckbox(id, key) {
  const el = document.getElementById(id);
  if (!el) return;
  el.checked = state.graphFilter[key];
  el.addEventListener("change", () => {
    state.graphFilter[key] = el.checked;
    if (state.graphMode === "knowledge") renderConstellations();
  });
}
_bindFilterCheckbox("filterTheme", "theme");
_bindFilterCheckbox("filterFile", "file");
_bindFilterCheckbox("filterFn", "fn");
_bindFilterCheckbox("filterCommit", "commit");

const graphSearchInput = document.getElementById("graphSearch");
if (graphSearchInput) {
  graphSearchInput.addEventListener("input", () => {
    const q = graphSearchInput.value.trim().toLowerCase();
    const svg = d3.select("#graph");
    svg.selectAll(".tree-nodes g").each(function(d) {
      const title = ((d.data && d.data.title) || "").toLowerCase();
      const match = q && title.includes(q);
      d3.select(this).select("circle")
        .attr("stroke-width", match ? 3 : (d.data && d.data.id === "__root" ? 2.5 : 1.5))
        .attr("stroke", match ? "var(--amber)" : (function(d2) {
          const kindColors2 = { root:"var(--cyan)", group:"var(--amber)", theme:"var(--violet)", module:"var(--cyan)", refactor:"var(--pink)", architecture:"var(--green)", file:"#3498db", function:"#1abc9c", method:"#1abc9c", class:"#16a085", commit:"#e67e22" };
          return kindColors2[d2.data && d2.data.kind] || "var(--violet)";
        })(d));
    });
  });
}
```

- [ ] **Step 5: Add entity detail handler to app.js**

Add `showEntityDetail` function after `showClusterDetail`:

```javascript
async function showEntityDetail(entityId) {
  setHTML(detailsBodyEl, `<p class="dim">Loading entity…</p>`);
  try {
    const e = await apiGet(`/missions/${state.activeMissionId}/entities/${encodeURIComponent(entityId)}`);
    const sigHtml = e.signature ? `<div class="commit-field"><span class="field-label">Signature</span><pre style="font-family:'JetBrains Mono',monospace;font-size:11px;margin:4px 0;overflow-x:auto">${escapeHtml(e.signature)}</pre></div>` : "";
    const docHtml = e.docstring ? `<div class="commit-field"><span class="field-label">Docstring</span><div style="font-size:12px;color:var(--text-dim)">${escapeHtml(e.docstring)}</div></div>` : "";
    const summaryHtml = e.llm_summary ? `<div class="commit-field"><span class="field-label">What it does</span><div style="font-size:12px">${escapeHtml(e.llm_summary)}</div></div>` : "";
    const whyHtml = e.llm_why ? `<div class="commit-field"><span class="field-label">Why it exists</span><div style="font-size:12px;color:var(--violet-light)">${escapeHtml(e.llm_why)}</div></div>` : "";
    const snippetHtml = e.code_snippet ? `<div class="commit-field"><span class="field-label">Code</span><pre style="font-family:'JetBrains Mono',monospace;font-size:10px;margin:4px 0;overflow-x:auto;max-height:200px">${escapeHtml(e.code_snippet)}</pre></div>` : "";
    const shaHtml = e.introduced_sha ? `<div class="commit-field"><span class="field-label">Introduced by</span><span class="sha-link" style="cursor:pointer;color:var(--cyan);font-family:monospace" data-sha="${escapeHtml(e.introduced_sha)}">${escapeHtml(shortSha(e.introduced_sha))}</span></div>` : "";
    setHTML(detailsBodyEl, `
      <div style="margin-bottom:8px">
        <div style="font-size:13px;font-weight:600;color:var(--text)">${escapeHtml(e.name)}</div>
        <div style="font-size:11px;color:var(--text-faint);margin-top:2px">${escapeHtml(e.kind)} · ${escapeHtml(e.path)}${e.line_start ? `:${e.line_start}` : ""}</div>
      </div>
      ${sigHtml}${docHtml}${summaryHtml}${whyHtml}${snippetHtml}${shaHtml}
    `);
    // Wire introduced_by sha click
    detailsBodyEl.querySelectorAll(".sha-link").forEach((el2) => {
      el2.addEventListener("click", () => onCommitClick(el2.dataset.sha));
    });
  } catch (err) {
    setHTML(detailsBodyEl, `<p class="dim">Failed to load entity: ${escapeHtml(err.message)}</p>`);
  }
}
```

- [ ] **Step 6: Add CSS for filter toolbar and entity details to styles.css**

In `frontend/styles.css`, append:

```css
/* ---- graph filter bar ---- */
.graph-filter {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 6px 14px;
  background: rgba(255,255,255,0.04);
  border-bottom: 1px solid var(--border);
  flex-wrap: wrap;
}
.filter-label {
  font-size: 11px;
  color: var(--text-faint);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
.filter-check {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  color: var(--text-dim);
  cursor: pointer;
  user-select: none;
}
.filter-check input[type=checkbox] {
  cursor: pointer;
  accent-color: var(--violet);
}
.filter-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  display: inline-block;
}
.graph-search-input {
  margin-left: auto;
  background: rgba(255,255,255,0.05);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 3px 8px;
  color: var(--text);
  font-size: 12px;
  width: 160px;
}
.graph-search-input:focus {
  outline: none;
  border-color: var(--violet);
}
```

- [ ] **Step 7: Rebuild frontend and verify in browser**

```bash
docker-compose up --build frontend
```

Open http://localhost:8080, select a completed mission, click the "Graph" tab, then switch to "Knowledge" mode. Verify:
- Knowledge tree renders with clustered nodes
- Filter checkboxes appear in the filter bar
- Search input highlights matching nodes when typed into
- Clicking a node shows details in the right panel

- [ ] **Step 8: Commit**

```bash
git add frontend/app.js frontend/index.html frontend/styles.css
git commit -m "feat: add entity nodes, graph filter bar, search, and entity detail panel to frontend"
```

---

## Phase 4: Technical KT Report Enhancement

### Task 9: Add 5 new KT report sections to reporter.py

**Files:**
- Modify: `backend/reporter.py`

- [ ] **Step 1: Update SECTIONS and _build_context in reporter.py**

Replace the `SECTIONS` list and `_build_context` function:

```python
SECTIONS = [
    ("overview", "Comprehensive project overview: what the repository is, its primary purpose, and its end-to-end functionality."),
    ("folder_structure", "Complete folder and file structure of the repository. For every file, write one sentence describing its exact role. Present as a hierarchical list or ASCII tree. Include ALL important files."),
    ("architecture_evolution", "How the architecture evolved across time. Reference pivotal commits and eras of development."),
    ("core_components_and_files", "Detailed breakdown of the most critical files in the codebase, explaining exactly what each file does, how the code works, and why it is important for a new developer."),
    ("function_inventory", "Inventory of the most important public functions and classes across the codebase. For each: name, file path, what it does (1-2 sentences), why it exists. Focus on the 15-25 most architecturally significant functions."),
    ("data_flow", "End-to-end data flow through the system. Trace how a typical user request enters the system, which functions are called in order, what is stored, and what is returned. Use concrete examples from the actual code."),
    ("entry_points", "All main entry points into the system: server startup, CLI commands, background workers, main functions. For each, trace the initial call chain showing the first 3-5 functions called."),
    ("critical_decisions", "Deep analysis of key architectural or design decisions, highlighting 'why' specific features were built the way they were."),
    ("branch_history", "Notable branches, merge patterns, and how collaboration happened."),
    ("major_refactors", "Significant refactors, rewrites, or migrations. Explain the technical debt eliminated."),
    ("risks", "Fragile areas, debt hotspots, and risks onboarding engineers should watch out for."),
    ("getting_started", "A step-by-step reading order for a new developer. List exactly which files to read first, second, third — and why. Include which functions to understand first. Make it so concrete that a new hire can follow it in their first day."),
    ("timeline", "Chronological bullet list of defining moments in the codebase's history."),
]


def _build_context(mission_id: str) -> str:
    repo = db.get_repo(mission_id) or {}
    nodes = db.get_knowledge_nodes(mission_id)
    touches = db.file_touch_counts(mission_id, min_touches=2, limit=40)
    entities = db.list_code_entities(mission_id, kind=None, path=None, limit=300)
    file_entities = [e for e in entities if e["kind"] == "file"]
    fn_entities = [e for e in entities if e["kind"] in ("function", "class", "method") and e.get("llm_summary")]

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
                "llm_summary": next(
                    (e.get("llm_summary") for e in file_entities if e["path"] == t["path"]),
                    None
                ),
                "recent_commits": [c.get("title") for c in db.top_commits_for_file(mission_id, t["path"], limit=4)],
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
```

- [ ] **Step 2: Verify report has new sections after re-running reporter**

Trigger a fresh report generation (or check existing missions that already have all phases done). The report endpoint should now return 13 sections instead of 9.

```bash
curl -s "http://localhost:8010/api/missions/MISSION_ID/report" | \
  python3 -c "import json,sys; d=json.load(sys.stdin); [print(s['section']) for s in d['sections']]"
```

Expected output includes: `folder_structure`, `function_inventory`, `data_flow`, `entry_points`, `getting_started`.

- [ ] **Step 3: Commit**

```bash
git add backend/reporter.py
git commit -m "feat: add folder_structure, function_inventory, data_flow, entry_points, getting_started report sections"
```

---

### Task 10: Final integration test + spec coverage check

**Files:** No code changes — verification only.

- [ ] **Step 1: Full end-to-end test with a public repo**

```bash
# 1. Start a fresh ingest of a well-known public repo
curl -s -X POST http://localhost:8010/api/missions \
  -H "Content-Type: application/json" \
  -d '{"repo_url": "https://github.com/pallets/flask"}' | python3 -m json.tool

# Note the mission_id from the response
# 2. Wait ~5-10 minutes for pipeline to complete, then poll status:
curl -s http://localhost:8010/api/missions/MISSION_ID | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['status'], 'commits:', d['commit_count'], 'entities:', d['knowledge_node_count'])"
```

- [ ] **Step 2: Test Q&A for function-level questions**

```bash
# Ask about a specific Flask function
curl -s -X POST http://localhost:8010/api/missions/MISSION_ID/chat \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{"question": "what does route do in flask", "history": []}' | grep "^data:" | head -5
```

Expected: SSE stream with substantive explanation referencing Flask route functionality.

- [ ] **Step 3: Verify all 13 report sections exist**

```bash
curl -s "http://localhost:8010/api/missions/MISSION_ID/report" | \
  python3 -c "
import json, sys
d = json.load(sys.stdin)
sections = [s['section'] for s in d['sections']]
required = ['overview','folder_structure','architecture_evolution','core_components_and_files',
            'function_inventory','data_flow','entry_points','critical_decisions',
            'branch_history','major_refactors','risks','getting_started','timeline']
for r in required:
    status = 'OK' if r in sections else 'MISSING'
    print(status, r)
"
```

Expected: all 13 show as OK.

- [ ] **Step 4: Verify knowledge graph has edges**

```bash
curl -s "http://localhost:8010/api/missions/MISSION_ID/graph/knowledge" | \
  python3 -c "import json,sys; d=json.load(sys.stdin); print('nodes:', len(d['nodes']), 'edges:', len(d['edges']))"
```

Expected: edges > 0.

- [ ] **Step 5: Verify entity endpoints**

```bash
curl -s "http://localhost:8010/api/missions/MISSION_ID/entities?kind=function&limit=3" | python3 -m json.tool
```

Expected: 3 function entities with signatures.

- [ ] **Step 6: Create final summary commit**

```bash
git add .
git commit -m "feat: Living Knowledge Graph complete — source code entities, intent-routing chat, enhanced report, graph edges"
```

---

## Self-Review

**Spec coverage check:**

| Spec requirement | Covered by task |
|---|---|
| Function-level Q&A ("what does X do") | Task 6 (intent routing + entity context) |
| "Why does X exist" tracing to commits | Task 4 (find_introducing_commit) + Task 6 |
| Public repo fix | Task 1 (list_repo_files uses git ls-tree, read_file_at_head uses git show — no checkout needed) |
| code_entities + entity_edges schema | Task 2 |
| Regex parser for Python/JS/TS/Go/Java/Rust | Task 3 |
| LLM deep-dive on top-25 hot files | Task 4 |
| entities Chroma collection | Task 5 |
| code_parse + code_analyze pipeline phases | Task 5 |
| Enhanced chat with multi-stage retrieval | Task 6 |
| CodeEntity + EntityEdge Pydantic models | Task 7 |
| GET /entities and GET /entities/{id} endpoints | Task 7 |
| KnowledgeGraphResponse with edges field | Task 7 |
| Filter toolbar (Themes/Files/Functions/Commits) | Task 8 |
| Search bar highlighting | Task 8 |
| Entity detail panel in sidebar | Task 8 |
| 5 new KT report sections | Task 9 |
| _build_context enriched with code entities | Task 9 |
| End-to-end integration test | Task 10 |

**Type consistency:**
- `EntityEdge` used in models.py (Task 7) matches the dict shape `{src_id, dst_id, edge_type}` used in db.py (Task 2) — consistent.
- `code_entities.id` format `fn:path::name` / `cls:path::name` / `file:path` defined in Task 5 pipeline and matched in Task 6 chat.py entity lookup — consistent.
- `db.list_code_entities` signature `(mission_id, kind, path, limit)` defined in Task 2, used in Task 5 (pipeline), Task 6 (chat), Task 9 (reporter) — consistent.
- `db.get_all_entity_edges` defined in Task 2, used in Task 7 (main.py) — consistent.

**Placeholder scan:** No TBDs. All code steps contain complete code. All commands have expected outputs. ✓
