from pydantic import BaseModel
from typing import Any


class CreateMissionRequest(BaseModel):
    repo_url: str
    github_token: str | None = None


class CreateMissionResponse(BaseModel):
    mission_id: str


class MissionSummary(BaseModel):
    mission_id: str
    url: str
    status: str
    default_branch: str | None = None
    head_sha: str | None = None
    created_at: str
    commit_count: int = 0
    key_commit_count: int = 0
    knowledge_node_count: int = 0


class CommitNode(BaseModel):
    sha: str
    parents: list[str]
    date: str
    author: str | None = None
    decision_type: str | None = None
    is_merge: bool = False
    is_key: bool = False
    branch_hint: str | None = None
    title: str | None = None


class CommitGraphResponse(BaseModel):
    commits: list[CommitNode]
    branches: list[dict[str, Any]]


class CommitFile(BaseModel):
    path: str
    change_type: str
    additions: int
    deletions: int


class CommitDetail(BaseModel):
    sha: str
    parents: list[str]
    date: str
    author_name: str | None = None
    author_email: str | None = None
    message: str
    is_merge: bool
    files_changed: int
    insertions: int
    deletions: int
    decision_type: str | None = None
    is_key: bool
    branch_hint: str | None = None
    files: list[CommitFile]
    title: str | None = None
    why: str | None = None
    impact: str | None = None
    modules: list[str] = []
    tags: list[str] = []
    risk: str | None = None
    confidence: float | None = None
    diff: str | None = None


class KnowledgeNode(BaseModel):
    id: str
    kind: str
    title: str
    summary: str
    member_shas: list[str]
    first_date: str | None = None
    last_date: str | None = None


class KnowledgeGraphResponse(BaseModel):
    nodes: list[KnowledgeNode]


class ReportSection(BaseModel):
    section: str
    content: str
    refs: list[dict[str, Any]] = []


class ReportResponse(BaseModel):
    sections: list[ReportSection]


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    question: str
    history: list[ChatMessage] = []


class ChatCitations(BaseModel):
    shas: list[str] = []
    files: list[str] = []
    branches: list[str] = []
    clusters: list[str] = []
