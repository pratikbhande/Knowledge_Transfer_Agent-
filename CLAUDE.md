# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Run / Build

The project is **Docker-only for development**. Backend code contains hardcoded container paths (`/app/missions`, `/app/chroma_db`) and will not run outside its container without modification.

```bash
cp .env.example .env            # fill in ANTHROPIC_API_KEY (required), GITHUB_TOKEN (recommended)
docker-compose up --build       # backend + frontend
docker-compose up --build backend   # backend only (code hot-reloads via volume mount + uvicorn --reload)
docker-compose logs -f backend  # tail backend logs
```

Ports (note the shift):
- Frontend (nginx): **http://localhost:8080**
- Backend (FastAPI): **http://localhost:8010** — host `8010` maps to container `8000`. The frontend hardcodes `http://localhost:8010/api` in `frontend/app.js:1`; change it there if you remap the port.

There are **no tests, no linter, and no build step** (frontend is vanilla JS served as static files by nginx).

## Architecture

COSMOBASE ingests a GitHub repo, runs an LLM over each commit to extract architectural decisions, writes the result to disk, and exposes a RAG chat over it. The pipeline has two agents (NOVA = ingestion, ORION = chat) that communicate through two persistence layers (mission JSON + Chroma collection).

### Request lifecycle

1. `POST /api/mission/start` → constructs a `NovaMission`, stores it in the in-memory `active_missions` dict, returns a `mission_id` (`backend/main.py:31`).
2. `GET /api/mission/{id}/stream` → SSE endpoint that drives `NovaMission.execute_mission()` and streams `log` / `node_added` / `mission_complete` events to the browser. When the generator finishes, the mission is **deleted from `active_missions`** — it's a one-shot stream.
3. NOVA writes final state to `./missions/<mission_id>.json` (mounted at `/app/missions` in the container) and indexes nodes into a Chroma collection **named after the mission_id**.
4. `POST /api/mission/{id}/orion` → constructs a fresh `OrionAgent`, which reads the mission JSON for repo name and queries the per-mission Chroma collection for RAG context, then streams Claude output back as SSE.

Because `active_missions` is an in-memory dict, restarting the backend mid-ingestion drops the stream and the NOVA work is lost. Completed missions survive because they live in `missions/*.json` + `chroma_db/`.

### Component map

- `backend/main.py` — FastAPI app, SSE wiring, CORS (`*`).
- `backend/nova.py` — `NovaMission`: orchestrates harvest → per-commit LLM analysis → vector indexing → debrief generation. The mission loop calls the LLM **once per commit sequentially**; this is the main runtime bottleneck and the reason `patch_text` is truncated to 1500 chars in the harvester.
- `backend/github_harvester.py` — PyGithub wrapper. `get_signals()` walks commits on the default branch oldest-first and fetches file patches per commit; `handle_rate_limit()` sleeps synchronously when GitHub core quota falls below 5. Running without a `GITHUB_TOKEN` hits the 60 req/hr anonymous limit almost immediately on any non-trivial repo.
- `backend/llm_client.py` — Unified Anthropic-primary / OpenAI-fallback client. Both `analyze_commit` (structured JSON extraction for NOVA) and `construct_orion_stream` (ORION chat) live here. Models are hardcoded: `claude-3-5-sonnet-20241022` and `gpt-4o`. `_parse_json` hand-strips markdown fences because the Anthropic path doesn't use a structured-output mode.
- `backend/vector_store.py` — Chroma + `sentence-transformers` (`all-MiniLM-L6-v2`). One collection per `mission_id`. Only nodes with `type in ("signal", "supernova")` are embedded; the root node is skipped.
- `backend/orion.py` — `OrionAgent`: reads `missions/<id>.json` for repo metadata, calls `VectorStore.search` (top-10), injects the results into a system prompt that enforces the "signals / transmissions / orbits / supernovas / astronauts" terminology, and streams Claude's reply.
- `backend/report_generator.py` — Builds the final mission debrief (narrative + health assessment) via two extra `generate_report` calls.
- `frontend/app.js` — Single-file D3.js v7 UI. `addNodeToTree` intentionally **builds a linear chain** by attaching each new node to the previously-streamed one instead of a real parent — the "tree" is a timeline, not a real git DAG. If you need a real parent/child structure, this is the place to change it.
- `frontend/index.html`, `frontend/styles.css` — Static assets served by nginx. No bundler, no package.json.

### Domain vocabulary (used throughout code and prompts)

NOVA and ORION deliberately rename git concepts. This vocabulary appears in variable names, node `type` fields, and LLM system prompts — **keep it consistent** when editing:

| Code term       | Git concept                 |
|-----------------|-----------------------------|
| signal          | commit                      |
| transmission    | pull request                |
| orbit           | branch                      |
| supernova       | major architectural commit  |
| star            | file                        |
| astronaut       | contributor                 |

### API keys

`backend/config.py` loads `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GITHUB_TOKEN` from the env. The `/api/mission/start` request body **also accepts keys inline** (`anthropic_key`, `openai_key`, `github_token`) — these override the env for that mission only. `OrionAgent` is constructed in `main.py` without per-request keys, so it always falls back to env vars.

## Gotchas

- Backend paths `/app/missions` and `/app/chroma_db` are hardcoded in `nova.py`, `orion.py`, and `vector_store.py`. Do not run `uvicorn` directly on the host without either creating those paths or refactoring them.
- The frontend uses `EventSource` for mission streams but a manual `fetch` + reader for ORION chat because it's a `POST`. The manual SSE parser in `sendOrionMessage` in `frontend/app.js` is fragile to chunk boundaries — chunks are split on `\n` and any partial JSON line is silently dropped.
- `active_missions` is per-process and not thread/worker safe. The Dockerfile runs a single uvicorn process, which is what keeps this working.
- `llm_client.py` catches all exceptions and prints them rather than raising — failures surface as a fallback decision node with `confidence: 0.0` and `tags: ["error"]`, not as an HTTP error.
