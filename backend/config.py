import os
from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # API keys (server-side only)
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    github_token: str | None = None

    # Data layout
    data_dir: str = "/app/data"

    # LLM models
    anthropic_model: str = "claude-3-5-sonnet-20241022"
    openai_model: str = "gpt-4o-mini"

    # Ingestion thresholds
    summarize_batch_size: int = 8
    key_commit_file_threshold: int = 8
    key_commit_line_threshold: int = 300
    narrative_sampling_stride: int = 25
    max_diff_bytes: int = 4000
    max_hunk_chars: int = 400

    # Retrieval
    chat_top_k: int = 8
    chat_diff_attach: int = 2

    class Config:
        env_file = ".env"


settings = Settings()


def _p(sub: str) -> str:
    path = Path(settings.data_dir) / sub
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


REPOS_DIR = _p("repos")
DB_DIR = _p("db")
CHROMA_DIR = _p("chroma")


HOTSPOT_PATHS = {
    "Dockerfile", "docker-compose.yml", "package.json", "package-lock.json",
    "yarn.lock", "pyproject.toml", "requirements.txt", "Pipfile", "Pipfile.lock",
    "go.mod", "go.sum", "Cargo.toml", "Cargo.lock", "pom.xml", "build.gradle",
    "Makefile", "CMakeLists.txt", "tsconfig.json", ".github/workflows",
    "schema.sql", "schema.prisma", "main.py", "main.go", "main.rs",
    "index.js", "index.ts", "app.py", "server.py",
}

HOTSPOT_KEYWORDS = (
    "refactor", "migrate", "rewrite", "breaking", "architect",
    "schema", "redesign", "restructure", "overhaul",
)
