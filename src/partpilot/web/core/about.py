"""What is running: package version, the two latest commits, and tool versions.

Read once at startup (``create_app``) and cached on ``app.state.about``: the code cannot change
without a restart, and the home page should not fork ``git`` on every visit. Everything degrades
quietly — no repository, no ``git`` binary, or a timeout just means an empty commit list.
"""

from __future__ import annotations

import platform
import sqlite3
import subprocess
from importlib import metadata
from pathlib import Path

from partpilot import __version__

COMMITS_SHOWN = 2


def _dist_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def recent_commits(repo_root: Path, n: int = COMMITS_SHOWN, timeout: float = 2.0) -> list[dict]:
    """``[{"hash", "date", "subject"}]`` newest first, or ``[]`` when git can't answer."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "log", f"-{n}", "--format=%h%x1f%ad%x1f%s", "--date=short"],
            capture_output=True, text=True, timeout=timeout, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    commits = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 3:
            commits.append({"hash": parts[0], "date": parts[1], "subject": parts[2]})
    return commits


def about_info(repo_root: Path) -> dict:
    tools = {"Python": platform.python_version(), "SQLite": sqlite3.sqlite_version}
    for dist in ("FastAPI", "Starlette", "uvicorn"):
        v = _dist_version(dist.lower())
        if v:
            tools[dist] = v
    return {"version": __version__, "commits": recent_commits(repo_root), "tools": tools}
