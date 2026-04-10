import logging
import traceback
import uuid
from urllib.parse import urlparse

log = logging.getLogger("cosmobase.pipeline")

import clusterer
import code_analyzer
import code_parser
import db
import embed
import git_ingest
import reporter
import summarizer
from config import settings
from git_ingest import RepoAuthError, repo_path
from llm import LLMClient


PHASES = [
    "clone", "walk", "classify", "select", "summarize",
    "cluster", "report", "index", "code_parse", "code_analyze", "done",
]


def _log(mission_id: str, level: str, phase: str, msg: str) -> None:
    db.log_event(mission_id, level, phase, msg)
    getattr(log, "warning" if level == "error" else level if hasattr(log, level) else "info")(
        "[%s][%s] %s", mission_id, phase, msg
    )


def _is_private_hint(url: str) -> bool:
    return urlparse(url).scheme in ("http", "https")


def create_mission(repo_url: str, github_token: str | None) -> str:
    mission_id = uuid.uuid4().hex[:10] + "_" + git_ingest.short_name(repo_url)
    db.init_schema(mission_id)
    db.create_repo(
        mission_id=mission_id,
        url=repo_url,
        is_private=bool(github_token),
        clone_path=repo_path(mission_id),
    )
    db.set_phase(mission_id, "queued", 0)
    _log(mission_id, "info", "queued", f"Mission queued for {repo_url}")
    return mission_id


def _phase_done(mission_id: str, phase: str) -> bool:
    rows = db.get_phases(mission_id)
    row = rows.get(phase)
    return bool(row and int(row.get("progress", 0)) >= 100 and not row.get("last_error"))


def run_mission(mission_id: str, github_token: str | None = None) -> None:
    repo = db.get_repo(mission_id)
    if not repo:
        raise RuntimeError(f"mission {mission_id} not found")
    url = repo["url"]
    clone_path = repo["clone_path"]
    llm = LLMClient()

    try:
        # ---- Phase 1: clone ----
        if not _phase_done(mission_id, "clone"):
            db.set_phase(mission_id, "clone", 10)
            _log(mission_id, "info", "clone", f"Cloning {url}")
            git_ingest.clone_or_fetch(url, github_token, clone_path)
            default = git_ingest.default_branch(clone_path)
            head = git_ingest.head_sha(clone_path, default)
            db.set_repo_meta(mission_id, default_branch=default, head_sha=head)
            branches = git_ingest.list_branches(clone_path, default)
            db.upsert_branches(mission_id, branches)
            db.set_phase(mission_id, "clone", 100)
            _log(mission_id, "success", "clone", f"Cloned. default={default} branches={len(branches)}")

        # ---- Phase 2: walk DAG ----
        if not _phase_done(mission_id, "walk"):
            db.set_phase(mission_id, "walk", 10)
            _log(mission_id, "info", "walk", "Walking commit graph")
            buf: list[dict] = []
            BATCH = 500
            total = 0
            for commit in git_ingest.walk_dag(clone_path):
                buf.append(commit)
                if len(buf) >= BATCH:
                    db.insert_commits(mission_id, buf)
                    total += len(buf)
                    buf.clear()
                    _log(mission_id, "info", "walk", f"Ingested {total} commits")
            if buf:
                db.insert_commits(mission_id, buf)
                total += len(buf)
            db.set_phase(mission_id, "walk", 100)
            _log(mission_id, "success", "walk", f"Walk complete: {total} commits")

        # ---- Phase 3: deterministic classify ----
        if not _phase_done(mission_id, "classify"):
            db.set_phase(mission_id, "classify", 50)
            summarizer.classify_all(mission_id)
            db.set_phase(mission_id, "classify", 100)

        # ---- Phase 4: key-commit selection ----
        if not _phase_done(mission_id, "select"):
            db.set_phase(mission_id, "select", 50)
            n = summarizer.select_key_commits(mission_id)
            db.set_phase(mission_id, "select", 100)
            _log(mission_id, "success", "select", f"Selected {n} key commits for LLM analysis")

        # ---- Phase 5: batched LLM summarization ----
        if not _phase_done(mission_id, "summarize"):
            db.set_phase(mission_id, "summarize", 0)
            _log(mission_id, "info", "summarize", "Running LLM summarization")

            def _cb(done: int, total: int) -> None:
                pct = int(done / max(1, total) * 100)
                db.set_phase(mission_id, "summarize", min(pct, 99))
                if done % (settings.summarize_batch_size * 4) == 0:
                    _log(mission_id, "info", "summarize", f"Analyzed {done}/{total} key commits")

            summarizer.run_summarization(mission_id, llm, clone_path, progress_cb=_cb)
            db.set_phase(mission_id, "summarize", 100)
            _log(mission_id, "success", "summarize", "Summarization complete")

        # ---- Phase 6: cluster knowledge nodes ----
        if not _phase_done(mission_id, "cluster"):
            db.set_phase(mission_id, "cluster", 30)
            _log(mission_id, "info", "cluster", "Clustering knowledge nodes")
            n = clusterer.cluster_knowledge_nodes(mission_id, llm)
            db.set_phase(mission_id, "cluster", 100)
            _log(mission_id, "success", "cluster", f"{n} knowledge nodes created")

        # ---- Phase 7: KT report sections ----
        if not _phase_done(mission_id, "report"):
            db.set_phase(mission_id, "report", 0)
            _log(mission_id, "info", "report", "Writing KT report sections")

            def _rcb(done: int, total: int) -> None:
                db.set_phase(mission_id, "report", int(done / max(1, total) * 100))
                _log(mission_id, "info", "report", f"Section {done}/{total} written")

            reporter.write_all(mission_id, llm, progress_cb=_rcb)
            db.set_phase(mission_id, "report", 100)
            _log(mission_id, "success", "report", "KT report complete")

        # ---- Phase 8: initial vector indexes ----
        if not _phase_done(mission_id, "index"):
            db.set_phase(mission_id, "index", 30)
            _log(mission_id, "info", "index", "Building retrieval indexes")
            counts = embed.build_indexes(mission_id)
            db.set_phase(mission_id, "index", 100)
            _log(mission_id, "success", "index", f"Indexed {counts}")

        # ---- Phase 9: parse source code entities (zero LLM calls) ----
        if not _phase_done(mission_id, "code_parse"):
            db.set_phase(mission_id, "code_parse", 10)
            _log(mission_id, "info", "code_parse", "Parsing source code entities")
            repo_info = db.get_repo(mission_id)
            clone_path_local = repo_info["clone_path"]
            files = git_ingest.list_repo_files(clone_path_local)
            all_entities: list[dict] = []
            all_edges: list[dict] = []
            file_entities: list[dict] = []

            # Get hot file paths for pickaxe priority
            hot = db.file_touch_counts(mission_id, min_touches=2, limit=20)
            hot_paths = {t["path"] for t in hot}

            for file_path in files:
                content = git_ingest.read_file_at_head(clone_path_local, file_path)
                if not content:
                    continue

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

                raw = code_parser.extract_entities(file_path, content)
                for e in raw:
                    kind_prefix = "cls" if e["kind"] == "class" else "fn"
                    e["id"] = f"{kind_prefix}:{file_path}::{e['name']}"
                    # Only run expensive pickaxe on top-20 hot files
                    if file_path in hot_paths:
                        try:
                            sha = git_ingest.find_introducing_commit(
                                clone_path_local, file_path, e["name"]
                            )
                            e["introduced_sha"] = sha
                        except Exception:
                            e["introduced_sha"] = None
                    else:
                        e["introduced_sha"] = None
                    all_entities.append(e)
                    all_edges.append({
                        "src_id": file_id,
                        "dst_id": e["id"],
                        "edge_type": "contains",
                    })

                # Import edges
                for imp in code_parser.extract_imports(file_path, content):
                    imp_clean = imp.strip("/").replace("-", "_").split(".")[0]
                    for other in files:
                        other_name = other.split("/")[-1].split(".")[0].replace("-", "_")
                        if other_name == imp_clean:
                            all_edges.append({
                                "src_id": file_id,
                                "dst_id": f"file:{other}",
                                "edge_type": "imports",
                            })
                            break

            # introduced_by edges for entities with a known sha
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

        # ---- Phase 10: LLM code analysis (2 LLM calls for 10 files) ----
        if not _phase_done(mission_id, "code_analyze"):
            db.set_phase(mission_id, "code_analyze", 10)
            _log(mission_id, "info", "code_analyze", "Running LLM analysis on hot files (batched)")
            n = code_analyzer.run_code_analysis(mission_id, llm)
            db.set_phase(mission_id, "code_analyze", 100)
            _log(mission_id, "success", "code_analyze", f"LLM analyzed {n} files")

            # Rebuild vector indexes to include enriched entity data
            db.set_phase(mission_id, "index", 30)
            counts = embed.build_indexes(mission_id)
            db.set_phase(mission_id, "index", 100)
            _log(mission_id, "success", "index", f"Re-indexed with entities: {counts}")

        db.set_phase(mission_id, "done", 100)
        _log(mission_id, "success", "done", "Mission complete")

    except RepoAuthError as e:
        db.set_phase(mission_id, "clone", 0, error=str(e))
        _log(mission_id, "error", "clone", f"Auth error: {e}")
        raise
    except Exception as e:
        tb = traceback.format_exc()
        log.error("[%s] pipeline error:\n%s", mission_id, tb)
        phases = db.get_phases(mission_id)
        current = next((p for p in reversed(PHASES) if p in phases), "unknown")
        db.set_phase(mission_id, current, phases.get(current, {}).get("progress", 0) or 0, error=str(e))
        _log(mission_id, "error", current, f"{e}")
