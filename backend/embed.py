from functools import lru_cache

import chromadb
from sentence_transformers import SentenceTransformer

import db
from config import CHROMA_DIR


_client: chromadb.PersistentClient | None = None


def _get_client() -> chromadb.PersistentClient:
    global _client
    if _client is None:
        _client = chromadb.PersistentClient(path=CHROMA_DIR)
    return _client


@lru_cache(maxsize=1)
def _get_model() -> SentenceTransformer:
    return SentenceTransformer("all-MiniLM-L6-v2")


def _col(mission_id: str, kind: str):
    name = f"{kind}_{mission_id}"
    return _get_client().get_or_create_collection(name=name, metadata={"hnsw:space": "cosine"})


def _embed(texts: list[str]) -> list[list[float]]:
    return _get_model().encode(texts).tolist()


def _reset_collection(mission_id: str, kind: str) -> None:
    try:
        _get_client().delete_collection(f"{kind}_{mission_id}")
    except Exception:
        pass


def index_commits(mission_id: str) -> int:
    analyses = db.all_analyses(mission_id)
    if not analyses:
        return 0
    ids: list[str] = []
    docs: list[str] = []
    metas: list[dict] = []
    for a in analyses:
        ids.append(a["sha"])
        tags = " ".join(a.get("tags") or [])
        modules = " ".join(a.get("modules") or [])
        docs.append(
            f"{a.get('title') or ''}\n{a.get('why') or ''}\n{a.get('impact') or ''}\n{tags}\n{modules}"
        )
        metas.append(
            {
                "sha": a["sha"],
                "date": a.get("date") or "",
                "author": a.get("author_name") or "",
                "decision_type": a.get("decision_type") or "",
            }
        )
    _reset_collection(mission_id, "commits")
    col = _col(mission_id, "commits")
    col.upsert(ids=ids, documents=docs, metadatas=metas, embeddings=_embed(docs))
    return len(ids)


def index_clusters(mission_id: str) -> int:
    nodes = db.get_knowledge_nodes(mission_id)
    if not nodes:
        return 0
    ids: list[str] = []
    docs: list[str] = []
    metas: list[dict] = []
    for n in nodes:
        ids.append(n["id"])
        docs.append(f"{n['title']}\n{n['summary']}")
        metas.append(
            {
                "kind": n.get("kind") or "theme",
                "first_date": n.get("first_date") or "",
                "last_date": n.get("last_date") or "",
                "members": ",".join(n.get("member_shas") or [])[:1000],
            }
        )
    _reset_collection(mission_id, "clusters")
    col = _col(mission_id, "clusters")
    col.upsert(ids=ids, documents=docs, metadatas=metas, embeddings=_embed(docs))
    return len(ids)


def index_files(mission_id: str) -> int:
    touches = db.file_touch_counts(mission_id, min_touches=2, limit=400)
    if not touches:
        return 0
    ids: list[str] = []
    docs: list[str] = []
    metas: list[dict] = []
    for t in touches:
        path = t["path"]
        top = db.top_commits_for_file(mission_id, path, limit=5)
        titles = " | ".join([(c.get("title") or "")[:80] for c in top if c.get("title")])
        ids.append(path)
        docs.append(f"{path}\ntouches={t['touches']}\n{titles}")
        metas.append(
            {
                "touches": int(t["touches"]),
                "adds": int(t["adds"] or 0),
                "dels": int(t["dels"] or 0),
            }
        )
    _reset_collection(mission_id, "files")
    col = _col(mission_id, "files")
    col.upsert(ids=ids, documents=docs, metadatas=metas, embeddings=_embed(docs))
    return len(ids)


def build_indexes(mission_id: str) -> dict:
    return {
        "commits": index_commits(mission_id),
        "clusters": index_clusters(mission_id),
        "files": index_files(mission_id),
    }


def _safe_query(mission_id: str, kind: str, query_emb: list[list[float]], k: int) -> list[dict]:
    try:
        col = _col(mission_id, kind)
        if col.count() == 0:
            return []
        n = min(k, col.count())
        res = col.query(query_embeddings=query_emb, n_results=n)
    except Exception:
        return []
    hits: list[dict] = []
    if not res or not res.get("ids"):
        return hits
    for i in range(len(res["ids"][0])):
        hits.append(
            {
                "source": kind,
                "id": res["ids"][0][i],
                "document": res["documents"][0][i],
                "metadata": res["metadatas"][0][i],
                "distance": res["distances"][0][i] if res.get("distances") else None,
            }
        )
    return hits


def search(mission_id: str, query: str, k: int = 8) -> list[dict]:
    query_emb = _embed([query])
    hits: list[dict] = []
    hits += _safe_query(mission_id, "commits", query_emb, k)
    hits += _safe_query(mission_id, "clusters", query_emb, max(2, k // 2))
    hits += _safe_query(mission_id, "files", query_emb, max(2, k // 2))
    hits.sort(key=lambda h: (h.get("distance") if h.get("distance") is not None else 1.0))
    return hits[:k]
