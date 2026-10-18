"""The home page's technical panel: version, two latest commits, tool versions."""

import subprocess

from partpilot import __version__
from partpilot.web.core.about import about_info


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@x", "PATH": "/usr/bin:/bin", "HOME": str(root)})


def test_about_reads_the_two_latest_commits(tmp_path):
    _git(tmp_path, "init", "-q")
    for i in (1, 2, 3):
        (tmp_path / f"f{i}").write_text("x")
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-q", "-m", f"Change number {i}")
    info = about_info(tmp_path)
    assert info["version"] == __version__
    assert [c["subject"] for c in info["commits"]] == ["Change number 3", "Change number 2"]
    assert len(info["commits"][0]["hash"]) >= 7 and len(info["commits"][0]["date"]) == 10   # YYYY-MM-DD


def test_about_without_a_repository_still_has_version_and_tools(tmp_path):
    info = about_info(tmp_path)
    assert info["commits"] == []
    assert info["version"] == __version__
    assert "Python" in info["tools"] and "SQLite" in info["tools"] and "FastAPI" in info["tools"]
    assert info["tools"]["Python"].count(".") >= 1
