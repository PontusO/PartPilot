"""Locate and render the user manual.

The manual is one Markdown file, ``docs/user-manual.md`` at the repository root, so the same
text reads well on GitHub and inside the app. ``PARTPILOT_MANUAL`` overrides the location for
packaged deployments. Rendering adds ``id`` anchors to every heading (``## Work orders`` →
``work-orders``) so other screens can deep-link to a chapter with ``/help#work-orders``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import markdown

_REPO_ROOT = Path(__file__).resolve().parents[5]
_EXTENSIONS = ["toc", "tables", "fenced_code", "sane_lists", "attr_list"]


def manual_path() -> Path:
    override = os.environ.get("PARTPILOT_MANUAL")
    return Path(override) if override else _REPO_ROOT / "docs" / "user-manual.md"


def slugify(text: str) -> str:
    """Match python-markdown's default heading slug: lowercase, non-word runs → ``-``."""
    text = re.sub(r"[^\w\s-]", "", text.strip().lower())
    return re.sub(r"[\s_-]+", "-", text).strip("-")


def toc(text: str) -> list[dict]:
    """Chapters (``##``) and their sections (``###``) in document order, with anchor slugs."""
    chapters: list[dict] = []
    for line in text.splitlines():
        if line.startswith("## "):
            title = line[3:].strip()
            chapters.append({"title": title, "slug": slugify(title), "sections": []})
        elif line.startswith("### ") and chapters:
            title = line[4:].strip()
            chapters[-1]["sections"].append({"title": title, "slug": slugify(title)})
    return chapters


def render(text: str) -> str:
    """Markdown → HTML with heading anchors. The top-level ``#`` title is left to the page."""
    md = markdown.Markdown(extensions=_EXTENSIONS, extension_configs={"toc": {"slugify": lambda v, _sep: slugify(v)}})
    return md.convert(text)


def load() -> tuple[str, list[dict]]:
    """Return ``(body_html, toc)`` for the manual, re-read on every call so edits show at once."""
    text = manual_path().read_text(encoding="utf-8")
    # Drop the document title line; the page renders its own <h1>.
    body = text.split("\n", 1)[1] if text.startswith("# ") else text
    return render(body), toc(body)
