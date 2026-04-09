import asyncio
import json
import threading
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse

import chat as chat_module
import db
import git_ingest
import pipeline
from git_ingest import RepoAuthError
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


app = FastAPI(title="COSMOBASE KT API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ----- helpers -----

def _ensure_mission(mission_id: str) -> dict:
    if not db.mission_exists(mission_id):
        raise HTTPException(status_code=404, detail="mission not found")
    repo = db.get_repo(mission_id)
    if not repo:
        raise HTTPException(status_code=404, detail="mission has no repo record")
    return repo


def _run_pipeline_bg(mission_id: str, token: str | None) -> None:
    try:
        pipeline.run_mission(mission_id, github_token=token)
    except RepoAuthError:
        pass
    except Exception as e:
        print(f"[main] pipeline crashed: {e}")


def _spawn_pipeline(mission_id: str, token: str | None) -> None:
    t = threading.Thread(target=_run_pipeline_bg, args=(mission_id, token), daemon=True)
    t.start()


# ----- endpoints -----

@app.get("/")
def root() -> dict[str, Any]:
    return {"service": "cosmobase-kt", "version": "2.0.0"}


@app.post("/api/missions", response_model=CreateMissionResponse)
async def create_mission(req: CreateMissionRequest) -> CreateMissionResponse:
    if not req.repo_url or not req.repo_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="repo_url must be an http(s) URL")

    mission_id = pipeline.create_mission(req.repo_url, req.github_token)

    # Probe auth synchronously so we can return 401 immediately for private repos without a token.
    try:
        git_ingest.clone_or_fetch(req.repo_url, req.github_token, db.get_repo(mission_id)["clone_path"])
        db.set_phase(mission_id, "clone", 100)
        db.log_event(mission_id, "success", "clone", "Clone verified")
        default = git_ingest.default_branch(db.get_repo(mission_id)["clone_path"])
        head = git_ingest.head_sha(db.get_repo(mission_id)["clone_path"], default)
        db.set_repo_meta(mission_id, default_branch=default, head_sha=head)
        branches = git_ingest.list_branches(db.get_repo(mission_id)["clone_path"], default)
        db.upsert_branches(mission_id, branches)
    except RepoAuthError as e:
        db.set_phase(mission_id, "clone", 0, error=str(e))
        db.log_event(mission_id, "error", "clone", str(e))
        raise HTTPException(
            status_code=401,
            detail="Authentication required. For private repos, provide a GitHub token.",
        ) from e
    except Exception as e:
        db.set_phase(mission_id, "clone", 0, error=str(e))
        db.log_event(mission_id, "error", "clone", str(e))
        raise HTTPException(status_code=400, detail=f"clone failed: {e}") from e

    _spawn_pipeline(mission_id, req.github_token)
    return CreateMissionResponse(mission_id=mission_id)


@app.get("/api/missions")
async def list_missions() -> dict[str, Any]:
    ids = db.list_missions_from_dir()
    out: list[dict[str, Any]] = []
    for mid in ids:
        try:
            repo = db.get_repo(mid)
            if not repo:
                continue
            out.append(
                {
                    "mission_id": mid,
                    "url": repo.get("url"),
                    "status": repo.get("status"),
                    "default_branch": repo.get("default_branch"),
                    "created_at": repo.get("created_at"),
                }
            )
        except Exception:
            continue
    out.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return {"missions": out}


@app.get("/api/missions/{mission_id}", response_model=MissionSummary)
async def get_mission(mission_id: str) -> MissionSummary:
    repo = _ensure_mission(mission_id)
    return MissionSummary(
        mission_id=mission_id,
        url=repo["url"],
        status=repo.get("status") or "unknown",
        default_branch=repo.get("default_branch"),
        head_sha=repo.get("head_sha"),
        created_at=repo.get("created_at") or "",
        commit_count=db.count_commits(mission_id),
        key_commit_count=db.count_key_commits(mission_id),
        knowledge_node_count=len(db.get_knowledge_nodes(mission_id)),
    )


@app.get("/api/missions/{mission_id}/events")
async def stream_events(mission_id: str, since: int = Query(0, ge=0)):
    _ensure_mission(mission_id)

    async def gen():
        last_id = since
        idle_ticks = 0
        while True:
            events = db.iter_events_since(mission_id, last_id, limit=200)
            for e in events:
                last_id = max(last_id, int(e["id"]))
                yield {
                    "event": e["level"],
                    "id": str(e["id"]),
                    "data": json.dumps(
                        {
                            "id": e["id"],
                            "ts": e["ts"],
                            "level": e["level"],
                            "phase": e["phase"],
                            "message": e["message"],
                        }
                    ),
                }
            repo = db.get_repo(mission_id) or {}
            status = repo.get("status")
            if status in ("done",):
                yield {"event": "done", "data": json.dumps({"status": status})}
                return
            if events:
                idle_ticks = 0
            else:
                idle_ticks += 1
                if idle_ticks > 600:  # ~10 min of silence
                    yield {"event": "heartbeat", "data": "{}"}
                    idle_ticks = 0
            await asyncio.sleep(1.0)

    return EventSourceResponse(gen())


@app.get("/api/missions/{mission_id}/graph", response_model=CommitGraphResponse)
async def get_graph(mission_id: str) -> CommitGraphResponse:
    _ensure_mission(mission_id)
    g = db.get_graph(mission_id)
    nodes = [
        CommitNode(
            sha=c["sha"],
            parents=c["parents"],
            date=c["date"],
            author=c.get("author_name"),
            decision_type=c.get("decision_type"),
            is_merge=bool(c.get("is_merge")),
            is_key=bool(c.get("is_key")),
            branch_hint=c.get("branch_hint"),
            title=c.get("title"),
        )
        for c in g["commits"]
    ]
    return CommitGraphResponse(commits=nodes, branches=g["branches"])


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


@app.get("/api/missions/{mission_id}/entities")
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


@app.get("/api/missions/{mission_id}/commits/{sha}", response_model=CommitDetail)
async def get_commit(mission_id: str, sha: str, with_diff: bool = True) -> CommitDetail:
    repo = _ensure_mission(mission_id)
    d = db.get_commit_detail(mission_id, sha)
    if not d:
        raise HTTPException(status_code=404, detail="commit not found")
    diff = None
    if with_diff:
        try:
            diff = git_ingest.read_diff(repo["clone_path"], sha)
        except Exception:
            diff = None
    return CommitDetail(
        sha=d["sha"],
        parents=d["parents"],
        date=d["date"],
        author_name=d.get("author_name"),
        author_email=d.get("author_email"),
        message=d["message"],
        is_merge=bool(d.get("is_merge")),
        files_changed=int(d.get("files_changed") or 0),
        insertions=int(d.get("insertions") or 0),
        deletions=int(d.get("deletions") or 0),
        decision_type=d.get("decision_type"),
        is_key=bool(d.get("is_key")),
        branch_hint=d.get("branch_hint"),
        files=d.get("files") or [],
        title=d.get("title"),
        why=d.get("why"),
        impact=d.get("impact"),
        modules=d.get("modules") or [],
        tags=d.get("tags") or [],
        risk=d.get("risk"),
        confidence=d.get("confidence"),
        diff=diff,
    )


@app.get("/api/missions/{mission_id}/report", response_model=ReportResponse)
async def get_report(mission_id: str) -> ReportResponse:
    _ensure_mission(mission_id)
    sections = db.get_report(mission_id)
    return ReportResponse(
        sections=[ReportSection(**s) for s in sections]
    )


@app.post("/api/missions/{mission_id}/chat")
async def chat_endpoint(mission_id: str, req: ChatRequest):
    _ensure_mission(mission_id)

    async def gen():
        history_dicts = [m.model_dump() for m in (req.history or [])]
        async for event in chat_module.chat_stream(mission_id, req.question, history_dicts):
            yield {"event": event["event"], "data": event["data"]}

    return EventSourceResponse(gen())
