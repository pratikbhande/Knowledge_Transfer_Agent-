import traceback
import uuid
from urllib.parse import urlparse

import clusterer
import db
import embed
import git_ingest
import reporter
import summarizer
from config import settings
from git_ingest import RepoAuthError, repo_path
from llm import LLMClient


PHASES = ["clone", "walk", "classify", "select", "summarize", "cluster", "report", "index", "done"]


def _log(mission_id: str, level: str, phase: str, msg: str) -> None:
    db.log_event(mission_id, level, phase, msg)


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

        # ---- Phase 8: vector indexes ----
        if not _phase_done(mission_id, "index"):
            db.set_phase(mission_id, "index", 30)
            _log(mission_id, "info", "index", "Building retrieval indexes")
            counts = embed.build_indexes(mission_id)
            db.set_phase(mission_id, "index", 100)
            _log(mission_id, "success", "index", f"Indexed {counts}")

        db.set_phase(mission_id, "done", 100)
        _log(mission_id, "success", "done", "Mission complete")

    except RepoAuthError as e:
        db.set_phase(mission_id, "clone", 0, error=str(e))
        _log(mission_id, "error", "clone", f"Auth error: {e}")
        raise
    except Exception as e:
        tb = traceback.format_exc()
        print(tb)
        phases = db.get_phases(mission_id)
        current = next((p for p in reversed(PHASES) if p in phases), "unknown")
        db.set_phase(mission_id, current, phases.get(current, {}).get("progress", 0) or 0, error=str(e))
        _log(mission_id, "error", current, f"{e}")
