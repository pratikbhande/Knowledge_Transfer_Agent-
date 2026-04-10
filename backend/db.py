import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from config import DB_DIR


SCHEMA = """
CREATE TABLE IF NOT EXISTS repo (
  mission_id TEXT PRIMARY KEY,
  url TEXT NOT NULL,
  is_private INTEGER NOT NULL,
  clone_path TEXT NOT NULL,
  default_branch TEXT,
  head_sha TEXT,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS branches (
  name TEXT PRIMARY KEY,
  head_sha TEXT NOT NULL,
  is_default INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS commits (
  sha TEXT PRIMARY KEY,
  parents TEXT NOT NULL,
  author_name TEXT,
  author_email TEXT,
  date TEXT NOT NULL,
  message TEXT NOT NULL,
  is_merge INTEGER NOT NULL,
  files_changed INTEGER NOT NULL,
  insertions INTEGER NOT NULL,
  deletions INTEGER NOT NULL,
  decision_type TEXT,
  is_key INTEGER NOT NULL DEFAULT 0,
  branch_hint TEXT,
  seq INTEGER
);

CREATE INDEX IF NOT EXISTS idx_commits_date ON commits(date);
CREATE INDEX IF NOT EXISTS idx_commits_key ON commits(is_key);

CREATE TABLE IF NOT EXISTS commit_files (
  sha TEXT NOT NULL,
  path TEXT NOT NULL,
  change_type TEXT NOT NULL,
  additions INTEGER NOT NULL,
  deletions INTEGER NOT NULL,
  PRIMARY KEY (sha, path)
);

CREATE INDEX IF NOT EXISTS idx_commit_files_path ON commit_files(path);

CREATE TABLE IF NOT EXISTS commit_analysis (
  sha TEXT PRIMARY KEY,
  title TEXT,
  why TEXT,
  impact TEXT,
  modules TEXT,
  tags TEXT,
  risk TEXT,
  confidence REAL,
  FOREIGN KEY (sha) REFERENCES commits(sha)
);

CREATE TABLE IF NOT EXISTS knowledge_nodes (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  summary TEXT NOT NULL,
  member_shas TEXT NOT NULL,
  first_date TEXT,
  last_date TEXT
);

CREATE TABLE IF NOT EXISTS report_sections (
  section TEXT PRIMARY KEY,
  content TEXT NOT NULL,
  refs TEXT
);

CREATE TABLE IF NOT EXISTS ingest_status (
  phase TEXT PRIMARY KEY,
  progress INTEGER NOT NULL,
  last_error TEXT
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  level TEXT NOT NULL,
  phase TEXT NOT NULL,
  message TEXT NOT NULL
);

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
"""


def db_path(mission_id: str) -> str:
    return str(Path(DB_DIR) / f"{mission_id}.sqlite")


def mission_exists(mission_id: str) -> bool:
    return os.path.exists(db_path(mission_id))


@contextmanager
def open_db(mission_id: str) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path(mission_id))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    # Always apply schema so new tables are available even on old missions
    conn.executescript(SCHEMA)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_schema(mission_id: str) -> None:
    with open_db(mission_id) as conn:
        conn.executescript(SCHEMA)


def create_repo(
    mission_id: str,
    url: str,
    is_private: bool,
    clone_path: str,
) -> None:
    with open_db(mission_id) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO repo(mission_id, url, is_private, clone_path, created_at, status) "
            "VALUES(?,?,?,?,?,?)",
            (
                mission_id,
                url,
                int(is_private),
                clone_path,
                datetime.now(timezone.utc).isoformat(),
                "queued",
            ),
        )


def set_repo_meta(
    mission_id: str,
    default_branch: str | None = None,
    head_sha: str | None = None,
    status: str | None = None,
) -> None:
    fields = []
    values: list[Any] = []
    if default_branch is not None:
        fields.append("default_branch=?")
        values.append(default_branch)
    if head_sha is not None:
        fields.append("head_sha=?")
        values.append(head_sha)
    if status is not None:
        fields.append("status=?")
        values.append(status)
    if not fields:
        return
    values.append(mission_id)
    with open_db(mission_id) as conn:
        conn.execute(f"UPDATE repo SET {','.join(fields)} WHERE mission_id=?", values)


def get_repo(mission_id: str) -> dict | None:
    with open_db(mission_id) as conn:
        row = conn.execute("SELECT * FROM repo WHERE mission_id=?", (mission_id,)).fetchone()
        return dict(row) if row else None


def list_missions_from_dir() -> list[str]:
    if not os.path.exists(DB_DIR):
        return []
    return [f.replace(".sqlite", "") for f in os.listdir(DB_DIR) if f.endswith(".sqlite")]


def upsert_branches(mission_id: str, branches: list[dict]) -> None:
    with open_db(mission_id) as conn:
        conn.execute("DELETE FROM branches")
        conn.executemany(
            "INSERT INTO branches(name, head_sha, is_default) VALUES(?,?,?)",
            [(b["name"], b["head_sha"], int(b.get("is_default", 0))) for b in branches],
        )


def insert_commits(mission_id: str, commits: Iterable[dict]) -> int:
    rows = []
    file_rows = []
    for c in commits:
        rows.append(
            (
                c["sha"],
                json.dumps(c.get("parents", [])),
                c.get("author_name"),
                c.get("author_email"),
                c["date"],
                c["message"],
                int(c.get("is_merge", 0)),
                int(c.get("files_changed", 0)),
                int(c.get("insertions", 0)),
                int(c.get("deletions", 0)),
                c.get("decision_type"),
                int(c.get("is_key", 0)),
                c.get("branch_hint"),
                c.get("seq"),
            )
        )
        for f in c.get("files", []):
            file_rows.append(
                (
                    c["sha"],
                    f["path"],
                    f.get("change_type", "M"),
                    int(f.get("additions", 0)),
                    int(f.get("deletions", 0)),
                )
            )
    with open_db(mission_id) as conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO commits
            (sha, parents, author_name, author_email, date, message, is_merge,
             files_changed, insertions, deletions, decision_type, is_key, branch_hint, seq)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            rows,
        )
        if file_rows:
            conn.executemany(
                "INSERT OR REPLACE INTO commit_files(sha, path, change_type, additions, deletions) "
                "VALUES(?,?,?,?,?)",
                file_rows,
            )
    return len(rows)


def count_commits(mission_id: str) -> int:
    with open_db(mission_id) as conn:
        return conn.execute("SELECT COUNT(*) FROM commits").fetchone()[0]


def count_key_commits(mission_id: str) -> int:
    with open_db(mission_id) as conn:
        return conn.execute("SELECT COUNT(*) FROM commits WHERE is_key=1").fetchone()[0]


def mark_key(mission_id: str, shas: list[str]) -> None:
    if not shas:
        return
    with open_db(mission_id) as conn:
        conn.executemany("UPDATE commits SET is_key=1 WHERE sha=?", [(s,) for s in shas])


def get_pending_key_commits(mission_id: str, limit: int) -> list[dict]:
    with open_db(mission_id) as conn:
        rows = conn.execute(
            """
            SELECT c.sha, c.parents, c.author_name, c.date, c.message,
                   c.is_merge, c.files_changed, c.insertions, c.deletions, c.decision_type
            FROM commits c
            LEFT JOIN commit_analysis a ON a.sha = c.sha
            WHERE c.is_key=1 AND a.sha IS NULL
            ORDER BY c.seq ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["parents"] = json.loads(d["parents"])
            files = conn.execute(
                "SELECT path, change_type, additions, deletions FROM commit_files WHERE sha=? "
                "ORDER BY additions+deletions DESC LIMIT 12",
                (d["sha"],),
            ).fetchall()
            d["files"] = [dict(f) for f in files]
            result.append(d)
        return result


def save_analysis(mission_id: str, items: list[dict]) -> None:
    rows = [
        (
            i["sha"],
            i.get("title"),
            i.get("why"),
            i.get("impact"),
            json.dumps(i.get("modules", [])),
            json.dumps(i.get("tags", [])),
            i.get("risk"),
            float(i.get("confidence", 0.7)),
        )
        for i in items
    ]
    with open_db(mission_id) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO commit_analysis(sha, title, why, impact, modules, tags, risk, confidence) "
            "VALUES(?,?,?,?,?,?,?,?)",
            rows,
        )


def all_analyses(mission_id: str) -> list[dict]:
    with open_db(mission_id) as conn:
        rows = conn.execute(
            """
            SELECT c.sha, c.date, c.author_name, c.decision_type,
                   a.title, a.why, a.impact, a.modules, a.tags, a.risk, a.confidence
            FROM commit_analysis a JOIN commits c ON c.sha = a.sha
            ORDER BY c.seq ASC
            """
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["modules"] = json.loads(d["modules"] or "[]")
            d["tags"] = json.loads(d["tags"] or "[]")
            out.append(d)
        return out


def save_knowledge_nodes(mission_id: str, nodes: list[dict]) -> None:
    rows = [
        (
            n["id"],
            n.get("kind", "theme"),
            n["title"],
            n.get("summary", ""),
            json.dumps(n.get("member_shas", [])),
            n.get("first_date"),
            n.get("last_date"),
        )
        for n in nodes
    ]
    with open_db(mission_id) as conn:
        conn.execute("DELETE FROM knowledge_nodes")
        conn.executemany(
            "INSERT INTO knowledge_nodes(id, kind, title, summary, member_shas, first_date, last_date) "
            "VALUES(?,?,?,?,?,?,?)",
            rows,
        )


def get_knowledge_nodes(mission_id: str) -> list[dict]:
    with open_db(mission_id) as conn:
        rows = conn.execute("SELECT * FROM knowledge_nodes").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["member_shas"] = json.loads(d["member_shas"])
            out.append(d)
        return out


def save_report_section(mission_id: str, section: str, content: str, refs: list[dict]) -> None:
    with open_db(mission_id) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO report_sections(section, content, refs) VALUES(?,?,?)",
            (section, content, json.dumps(refs)),
        )


def get_report(mission_id: str) -> list[dict]:
    with open_db(mission_id) as conn:
        rows = conn.execute("SELECT * FROM report_sections").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["refs"] = json.loads(d["refs"] or "[]")
            out.append(d)
        return out


def set_phase(mission_id: str, phase: str, progress: int, error: str | None = None) -> None:
    with open_db(mission_id) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO ingest_status(phase, progress, last_error) VALUES(?,?,?)",
            (phase, progress, error),
        )
        conn.execute("UPDATE repo SET status=? WHERE mission_id=?", (phase, mission_id))


def get_phases(mission_id: str) -> dict[str, dict]:
    with open_db(mission_id) as conn:
        rows = conn.execute("SELECT * FROM ingest_status").fetchall()
        return {r["phase"]: dict(r) for r in rows}


def log_event(mission_id: str, level: str, phase: str, message: str) -> int:
    with open_db(mission_id) as conn:
        cur = conn.execute(
            "INSERT INTO events(ts, level, phase, message) VALUES(?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), level, phase, message),
        )
        return cur.lastrowid


def iter_events_since(mission_id: str, since_id: int, limit: int = 200) -> list[dict]:
    with open_db(mission_id) as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE id > ? ORDER BY id ASC LIMIT ?",
            (since_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_graph(mission_id: str) -> dict:
    with open_db(mission_id) as conn:
        branches = [dict(r) for r in conn.execute("SELECT * FROM branches").fetchall()]
        rows = conn.execute(
            """
            SELECT c.sha, c.parents, c.date, c.author_name, c.decision_type, c.is_merge,
                   c.is_key, c.files_changed, c.insertions, c.deletions, c.branch_hint,
                   a.title
            FROM commits c
            LEFT JOIN commit_analysis a ON a.sha = c.sha
            ORDER BY c.seq ASC
            """
        ).fetchall()
        commits = []
        for r in rows:
            d = dict(r)
            d["parents"] = json.loads(d["parents"])
            commits.append(d)
        return {"commits": commits, "branches": branches}


def get_commit_detail(mission_id: str, sha: str) -> dict | None:
    with open_db(mission_id) as conn:
        row = conn.execute(
            """
            SELECT c.*, a.title, a.why, a.impact, a.modules, a.tags, a.risk, a.confidence
            FROM commits c LEFT JOIN commit_analysis a ON a.sha = c.sha
            WHERE c.sha=?
            """,
            (sha,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["parents"] = json.loads(d["parents"])
        d["modules"] = json.loads(d["modules"]) if d.get("modules") else []
        d["tags"] = json.loads(d["tags"]) if d.get("tags") else []
        files = conn.execute(
            "SELECT path, change_type, additions, deletions FROM commit_files WHERE sha=?",
            (sha,),
        ).fetchall()
        d["files"] = [dict(f) for f in files]
        return d


def file_touch_counts(mission_id: str, min_touches: int = 2, limit: int = 200) -> list[dict]:
    with open_db(mission_id) as conn:
        rows = conn.execute(
            """
            SELECT path, COUNT(*) AS touches, SUM(additions) AS adds, SUM(deletions) AS dels
            FROM commit_files
            GROUP BY path
            HAVING touches >= ?
            ORDER BY touches DESC
            LIMIT ?
            """,
            (min_touches, limit),
        ).fetchall()
        return [dict(r) for r in rows]


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


def top_commits_for_file(mission_id: str, path: str, limit: int = 5) -> list[dict]:
    with open_db(mission_id) as conn:
        rows = conn.execute(
            """
            SELECT c.sha, c.date, a.title
            FROM commit_files f
            JOIN commits c ON c.sha = f.sha
            LEFT JOIN commit_analysis a ON a.sha = c.sha
            WHERE f.path = ?
            ORDER BY c.date DESC
            LIMIT ?
            """,
            (path, limit),
        ).fetchall()
        return [dict(r) for r in rows]
