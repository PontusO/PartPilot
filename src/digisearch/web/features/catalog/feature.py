"""Catalog feature descriptor."""

from __future__ import annotations

from pathlib import Path

from ...core import Feature, NavItem
from .migrations import MIGRATIONS
from .router import router

feature = Feature(
    name="catalog",
    router=router,
    nav=NavItem(label="Parts", url="/catalog", roles=None, icon="📦", order=10),
    migrations=MIGRATIONS,
    template_dir=Path(__file__).parent / "templates",
)
