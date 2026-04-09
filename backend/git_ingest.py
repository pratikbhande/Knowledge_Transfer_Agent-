import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse, urlunparse

from config import REPOS_DIR, settings


class RepoAuthError(Exception):
    pass


class GitError(Exception):
    pass


NUL = "\x00"
RS = "\x1e"  # record separator


def repo_path(mission_id: str) -> str:
    return str(Path(REPOS_DIR) / mission_id)


def short_name(url: str) -> str:
    p = urlparse(url.rstrip("/").rstrip(".git"))
    return (p.path or "repo").strip("/").replace("/", "_") or "repo"


def _auth_url(url: str, token: str | None) -> str:
    if not token:
        return url
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        return url
    netloc = f"x-access-token:{token}@{p.hostname}"
    if p.port:
        netloc += f":{p.port}"
    return urlunparse((p.scheme, netloc, p.path, p.params, p.query, p.fragment))


def _run(cmd: list[str], cwd: str | None = None, timeout: int = 600) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        return subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise GitError(f"git command timed out: {' '.join(cmd)}") from e


def clone_or_fetch(url: str, token: str | None, target: str) -> None:
    os.makedirs(REPOS_DIR, exist_ok=True)
    if os.path.exists(os.path.join(target, ".git")):
        r = _run(["git", "-c", "credential.helper=", "-C", target, "fetch", "--all", "--prune", "--tags"])
        if r.returncode != 0:
            _check_auth_error(r.stderr, url)
            raise GitError(f"git fetch failed: {r.stderr.strip()}")
        return

    if os.path.exists(target):
        shutil.rmtree(target, ignore_errors=True)

    auth = _auth_url(url, token)
    r = _run(
        [
            "git",
            "-c",
            "credential.helper=",
            "clone",
            "--filter=blob:none",
            "--no-checkout",
            "--quiet",
            auth,
            target,
        ],
        timeout=1800,
    )
    if r.returncode != 0:
        _check_auth_error(r.stderr, url)
        raise GitError(f"git clone failed: {r.stderr.strip()}")

    # Scrub the auth URL from the config so the token never persists on disk.
    _run(["git", "-C", target, "remote", "set-url", "origin", url])


def _check_auth_error(stderr: str, url: str) -> None:
    needles = (
        "Authentication failed",
        "could not read Username",
        "fatal: Authentication",
        "Repository not found",
        "403",
        "401",
    )
    s = stderr or ""
    if any(n.lower() in s.lower() for n in needles):
        raise RepoAuthError(f"Authentication failed for {url}")


def default_branch(path: str) -> str:
    r = _run(["git", "-C", path, "symbolic-ref", "--short", "HEAD"])
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    r = _run(["git", "-C", path, "rev-parse", "--abbrev-ref", "origin/HEAD"])
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip().removeprefix("origin/")
    r = _run(["git", "-C", path, "remote", "show", "origin"])
    m = re.search(r"HEAD branch:\s*(\S+)", r.stdout or "")
    if m:
        return m.group(1)
    return "main"


def head_sha(path: str, ref: str = "HEAD") -> str:
    r = _run(["git", "-C", path, "rev-parse", ref])
    return r.stdout.strip() if r.returncode == 0 else ""


def list_branches(path: str, default: str) -> list[dict]:
    r = _run(
        [
            "git",
            "-C",
            path,
            "for-each-ref",
            "--format=%(refname:short)%x09%(objectname)",
            "refs/heads",
            "refs/remotes",
        ]
    )
    out: list[dict] = []
    seen: set[str] = set()
    for line in (r.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        name, sha = parts
        if name.endswith("/HEAD"):
            continue
        clean = name.removeprefix("origin/")
        if clean in seen:
            continue
        seen.add(clean)
        out.append({"name": clean, "head_sha": sha, "is_default": 1 if clean == default else 0})
    return out


def walk_dag(path: str) -> Iterator[dict]:
    """Yield commits across all branches, oldest-first, with file stats."""
    fmt = f"%H%x00%P%x00%an%x00%ae%x00%aI%x00%s%x00%b{RS}"
    cmd = [
        "git",
        "-C",
        path,
        "log",
        "--all",
        "--reverse",
        "--numstat",
        "--no-renames",
        f"--format={fmt}",
    ]
    r = _run(cmd, timeout=3600)
    if r.returncode != 0:
        raise GitError(f"git log failed: {r.stderr.strip()}")

    text = r.stdout
    seq = 0
    for record in text.split(RS):
        record = record.strip("\n")
        if not record.strip():
            continue
        head, _, tail = record.partition("\n")
        parts = head.split(NUL)
        if len(parts) < 7:
            continue
        sha, parents_raw, an, ae, date, subject, body = parts[:7]
        parents = [p for p in parents_raw.split() if p]
        message = subject
        if body.strip():
            message = subject + "\n\n" + body.strip()

        files: list[dict] = []
        insertions = 0
        deletions = 0
        for line in tail.splitlines():
            line = line.strip()
            if not line:
                continue
            m = re.match(r"^(\-|\d+)\t(\-|\d+)\t(.+)$", line)
            if not m:
                continue
            adds = 0 if m.group(1) == "-" else int(m.group(1))
            dels = 0 if m.group(2) == "-" else int(m.group(2))
            files.append(
                {
                    "path": m.group(3),
                    "change_type": "M",
                    "additions": adds,
                    "deletions": dels,
                }
            )
            insertions += adds
            deletions += dels

        seq += 1
        yield {
            "sha": sha,
            "parents": parents,
            "author_name": an,
            "author_email": ae,
            "date": date,
            "message": message,
            "is_merge": 1 if len(parents) > 1 else 0,
            "files_changed": len(files),
            "insertions": insertions,
            "deletions": deletions,
            "files": files,
            "seq": seq,
        }


def read_diff(path: str, sha: str, max_bytes: int | None = None) -> str:
    cap = max_bytes or settings.max_diff_bytes
    r = _run(["git", "-C", path, "show", "--no-color", "--stat", "--patch", "--format=", sha])
    if r.returncode != 0:
        return ""
    text = r.stdout or ""
    if len(text) <= cap:
        return text
    return text[:cap] + f"\n... (diff truncated, {len(text) - cap} bytes omitted)"


_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", "vendor", ".venv", "venv",
    "dist", "build", ".next", ".nuxt", "target", ".tox", "coverage",
    ".mypy_cache", ".pytest_cache", "eggs", ".eggs",
}

_SOURCE_EXTS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".java", ".rs",
    ".rb", ".php", ".cs", ".cpp", ".c", ".h", ".hpp", ".swift",
    ".kt", ".scala", ".ex", ".exs",
}


def list_repo_files(path: str) -> list[str]:
    """Return all source file paths (relative to repo root) via git ls-tree. No checkout needed."""
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
    """Read file content at HEAD without a working tree checkout (uses git show)."""
    r = _run(["git", "-C", path, "show", f"HEAD:{file_path}"])
    if r.returncode != 0:
        return ""
    return r.stdout or ""


def find_introducing_commit(path: str, file_path: str, symbol_name: str) -> str | None:
    """Use git-log pickaxe (-S) to find the oldest commit that added symbol_name in file_path."""
    r = _run(
        ["git", "-C", path, "log", "--follow", "--format=%H", "-S", symbol_name, "--", file_path],
        timeout=30,
    )
    if r.returncode != 0 or not (r.stdout or "").strip():
        return None
    lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    return lines[-1] if lines else None  # oldest = last in reverse-chronological output


def delete_clone(mission_id: str) -> None:
    p = repo_path(mission_id)
    if os.path.exists(p):
        shutil.rmtree(p, ignore_errors=True)
