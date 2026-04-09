# Living Knowledge Graph KT Agent — Design Spec

**Date:** 2026-04-10  
**Status:** Approved for implementation  
**Author:** Pratik (Cosmobase KT Agent)

---

## Problem Statement

When a developer joins a large legacy codebase, they are overwhelmed because:
- No single source of truth for "how does this code work or why does it exist"
- Git history is cryptic without code context
- Existing KT sessions are manual, incomplete, and stale within weeks
- No tool connects source code entities to the git history that explains *why* they were built

Existing tools: Sourcegraph (code search only), GitClear (git analytics only), Confluence (manual docs). None combine all three layers into a searchable, conversational, visual system.

---

## Solution: Three-Layer Living Knowledge Graph

```
┌────────────────────────────────────────────────────────┐
│              KNOWLEDGE GRAPH (D3 Force-Directed)       │
│   Theme nodes ──► Module nodes ──► File nodes          │
│        │               │               │               │
│   belongs_to      imports edges   contains edges       │
│        │               │               │               │
│   Commit nodes ◄── introduced_by ── Function nodes     │
└────────────────────────────────────────────────────────┘
           ▲                     ▲
    Vector DB (Chroma)     SQLite (structured)
    semantic retrieval     graph traversal + filtering
```

**Layer 1 — Source Code Graph (NEW):** Regex-parsed entities (files, functions, classes, methods) stored as `code_entities` + `entity_edges` in SQLite. Connected by typed edges: `contains`, `imports`, `calls` (heuristic), `introduced_by` (git blame/pickaxe).

**Layer 2 — Git Evolution (existing, enhanced):** Commits linked to code entities. Every function knows which commit introduced it and how it evolved. Provides the "why it exists" answering capability.

**Layer 3 — Semantic Clusters (existing, enhanced):** LLM-clustered themes now also linked to the code entities they describe, creating edges between abstract "Feature: Authentication" and concrete `auth.py::validate_token`.

---

## Root Cause: Public Repo Failure

**Bug:** `git_ingest.py::clone_or_fetch` uses `--filter=blob:none --no-checkout`. This downloads tree objects but **no file blobs**, so source code is never available.

**Fix:** After clone completes, add `git checkout HEAD -- .` to trigger blob download for current HEAD only (efficient partial hydration). One-line fix.

---

## Pipeline Extension

Extend `pipeline.py::PHASES` with two new phases after `index`:

### Phase `code_parse`
- Walk checked-out file tree
- Per file: detect language by extension, run regex extractor
- Extracts: `{kind, name, path, signature, docstring, snippet, line_start, line_end}`
- For each function/class: run `git log --follow -S "name" -- path` (pickaxe) to find introducing commit
- Build `imports` edges via regex on import statements
- Build `contains` edges (file→function, module→file)
- Store in `code_entities` + `entity_edges`

### Phase `code_analyze`
- Select top 25 files by touch count (hottest files = most important)
- Send full file content + commit history context to LLM
- LLM returns: `{summary, why_it_exists, key_functions:[{name, purpose, why}]}`
- Store `llm_summary` + `llm_why` per entity
- Index all entities into `entities_{mission_id}` Chroma collection (id = entity id, doc = name + signature + docstring + llm_summary)

---

## Data Model

### New tables in per-mission SQLite DB

```sql
CREATE TABLE IF NOT EXISTS code_entities (
  id TEXT PRIMARY KEY,           -- "fn:path/file.py::func_name" or "cls:..." or "file:..."
  kind TEXT NOT NULL,            -- "file" | "function" | "class" | "method"
  name TEXT NOT NULL,
  path TEXT NOT NULL,
  signature TEXT,                -- e.g. "def run_mission(mission_id: str, token: str) -> None"
  docstring TEXT,
  code_snippet TEXT,             -- first 400 chars of function/class body
  llm_summary TEXT,              -- LLM: what does this entity do
  llm_why TEXT,                  -- LLM: why does this entity exist / what problem does it solve
  introduced_sha TEXT,           -- SHA of commit that first added this entity (git pickaxe)
  line_start INTEGER,
  line_end INTEGER
);

CREATE INDEX IF NOT EXISTS idx_entities_path ON code_entities(path);
CREATE INDEX IF NOT EXISTS idx_entities_kind ON code_entities(kind);

CREATE TABLE IF NOT EXISTS entity_edges (
  src_id TEXT NOT NULL,
  dst_id TEXT NOT NULL,
  edge_type TEXT NOT NULL,       -- "contains" | "imports" | "calls" | "introduced_by" | "belongs_to"
  PRIMARY KEY (src_id, dst_id, edge_type)
);

CREATE INDEX IF NOT EXISTS idx_edges_src ON entity_edges(src_id);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON entity_edges(dst_id);
```

### Chroma collections (per mission)
| Collection | Contents | Use |
|---|---|---|
| `commits_{id}` | commit summaries (existing) | evolution, "when was X added" |
| `clusters_{id}` | knowledge cluster descriptions (existing) | theme/feature overview |
| `files_{id}` | file touch stats + commit titles (existing) | file-level questions |
| `entities_{id}` | function/class name + signature + docstring + llm_summary (NEW) | "what does X do" |

---

## Enhanced RAG Chat

### Intent Detection
Keyword routing before vector search. Detects intent from question:

| Intent | Keywords | Primary Source |
|---|---|---|
| `entity_explain` | "what does X do", "how does X work", "explain X function" | `entities` collection → code snippet + llm_summary |
| `entity_origin` | "why does X exist", "why was X added", "who wrote X" | `entities` → `introduced_sha` → commit analysis → cluster |
| `file_evolution` | "how has X evolved", "history of X file", "changes to X" | `files` + `commits` collection for that path |
| `onboarding` | "where to start", "what to read first", "how to set up" | `clusters` + report `getting_started` section |
| `general` | everything else | current multi-collection RAG |

### Context Assembly for entity_explain
```
[ENTITY fn:backend/pipeline.py::run_mission]
Signature: def run_mission(mission_id: str, github_token: str | None) -> None
Summary: Orchestrates the full 8-phase ingestion pipeline for a repo mission.
Why it exists: Central coordinator that sequences all pipeline phases and handles error recovery.
Code:
  def run_mission(...):
    repo = db.get_repo(mission_id)
    ...

[INTRODUCING COMMIT sha:abc123 date:2024-01-15]
Title: Add pipeline orchestration with phase-based error recovery
Why: Needed a single entry point to sequence clone→walk→classify→summarize→cluster→report→index
```

---

## Knowledge Graph Visualization

### Node Types

| Type | Shape | Color | Size | Represents |
|---|---|---|---|---|
| `module` | rounded rect | green | 40px | Top-level directory |
| `theme` | hexagon | purple | 35px | Semantic cluster |
| `file` | circle | blue | 22px | Source file |
| `function` | circle | cyan | 12px | Function / method |
| `class` | circle | teal | 16px | Class |
| `commit` | square | orange | 8px | Key commit node |

### Edge Types

| Type | Style | Color | Meaning |
|---|---|---|---|
| `contains` | solid | gray | module→file, file→function/class |
| `imports` | dashed | blue | file→file import dependency |
| `introduced_by` | dotted | orange | function→commit (birth commit) |
| `belongs_to` | solid thick | purple | commit/file→theme cluster |

### Graph Modes
- **Knowledge Tree** (default): Hierarchical top-down layout. Themes at top, modules second, files third, functions at leaf level.
- **Dependency Web**: Force-directed layout showing import/call relationships between files.

### Interaction
- Click any node → details panel: full info (code snippet, LLM summary, why, introducing commit, cluster membership)
- Double-click module or file node → expand/collapse children in-place
- Filter toolbar (top-right): checkboxes to show/hide each node type
- Search bar: type name → matching nodes highlighted, graph pans to first match
- Right-click node → "Show history" (switches to commit tab, filtered to this entity)

---

## Technical KT Report Sections

### New Sections

| Section key | Content |
|---|---|
| `folder_structure` | ASCII directory tree with one-line role per file, generated from `code_entities` |
| `function_inventory` | All public functions/classes across hot files with LLM-written purpose + "why it exists" |
| `data_flow` | End-to-end data flow: request enters → what functions are called → what is stored/returned |
| `entry_points` | All main entry points (main.py, app.py, server.py, index.js) with their call chains |
| `getting_started` | Ordered reading list: "read these 5 files in this exact order, here is why" |

### Enhanced Existing Sections
- `core_components_and_files`: Now pulls actual `llm_summary` + `code_snippet` from `code_entities` instead of only commit history
- `critical_decisions`: Now links decisions to specific functions via `entity_edges.belongs_to`
- `onboarding`: Enhanced with ordered reading list from `getting_started` + function entry points

---

## New API Endpoints

```
GET /api/missions/{id}/entities
  Query params: kind (file|function|class), path (filter by file path)
  Returns: list of code entities with id, kind, name, path, signature, llm_summary

GET /api/missions/{id}/entities/{entity_id}
  Returns: full entity detail — code_snippet, docstring, llm_summary, llm_why,
           introduced_sha, all outgoing/incoming edges

GET /api/missions/{id}/graph/knowledge
  ENHANCED: returns nodes[] AND edges[] (was nodes-only before)
  Edge format: {src_id, dst_id, edge_type}
```

Chat endpoint `POST /api/missions/{id}/chat` — unchanged externally, enhanced internally.

---

## Implementation Phases

### Phase 1: Fix Public Repo + Source Code Ingestion (Critical)
1. Fix `clone_or_fetch` → add `git checkout HEAD -- .` post-clone
2. Add `code_entities` + `entity_edges` tables to `db.py::SCHEMA`
3. New `code_parser.py` — regex-based extractor for Python, JS/TS, Go, Java, Rust
4. New `code_analyzer.py` — LLM deep-dive on top 25 hot files
5. Add `code_parse` + `code_analyze` phases to `pipeline.py`
6. Add `entities` Chroma collection to `embed.py`

### Phase 2: Enhanced Chat (Intent Routing)
1. Update `chat.py` — intent detection + multi-stage retrieval
2. Update `embed.py::search` — add entities collection query
3. New context formatter for entity answers (code snippet + why)

### Phase 3: Knowledge Graph Edges + Visualization
1. Update `db.py::get_knowledge_graph` — return edges
2. Update `models.py::KnowledgeGraphResponse` — add edges field
3. Update `frontend/app.js` — replace linear chain with force-directed graph
4. Add node type shapes, colors, filter toolbar, search bar
5. Two graph modes (Knowledge Tree / Dependency Web)

### Phase 4: Technical KT Report Enhancement
1. Add 5 new sections to `reporter.py::SECTIONS`
2. Update `reporter.py::_build_context` to include code entity data
3. Update LLM prompts to reference actual code, not just commit history

---

## Files Changed

| File | Change |
|---|---|
| `backend/git_ingest.py` | Fix `clone_or_fetch` — add `git checkout HEAD` |
| `backend/db.py` | Add `code_entities`, `entity_edges` tables + new query functions |
| `backend/pipeline.py` | Add `code_parse`, `code_analyze` to PHASES + run loop |
| `backend/code_parser.py` | NEW — regex entity extractor per language |
| `backend/code_analyzer.py` | NEW — LLM file analysis + entity enrichment |
| `backend/embed.py` | Add `index_entities()`, update `search()` |
| `backend/chat.py` | Intent detection + multi-stage retrieval |
| `backend/reporter.py` | 5 new sections + enhanced context with code entities |
| `backend/llm.py` | New methods: `analyze_file()`, `explain_entity()` |
| `backend/models.py` | `CodeEntity`, `EntityEdge` models + update `KnowledgeGraphResponse` |
| `backend/main.py` | 2 new entity endpoints |
| `frontend/app.js` | Force-directed D3 graph + filter toolbar + search + two modes |
| `frontend/styles.css` | Node type colors, filter panel, entity detail styles |

---

## Non-Goals (Explicitly Excluded)

- No tree-sitter dependency (too complex, not needed for 90% of value)
- No call graph via dynamic analysis / execution tracing
- No support for private repos without token (current behavior preserved)
- No persistent memory across missions (each mission is fully self-contained)
- No multi-user auth / team features

---

## Success Criteria

1. Asking "what does `run_mission` do?" returns a clear answer with code snippet and LLM explanation
2. Asking "why does `run_mission` exist?" traces back to the introducing commit and explains the original motivation
3. A public GitHub repo (e.g. `https://github.com/tiangolo/fastapi`) can be fully ingested and chatted with
4. Knowledge graph shows interconnected nodes with visible edges, not a linear chain
5. KT report includes folder structure, function inventory, and an ordered "where to start" reading list
6. New developer can go from zero → productive understanding of any repo within 30 minutes of reading the KT report + chatting with the agent
