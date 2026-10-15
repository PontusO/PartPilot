"""Documents feature descriptor."""

from __future__ import annotations

from pathlib import Path

from ...core import Feature, NavItem
from .migrations import MIGRATIONS
from .router import router

feature = Feature(
    name="documents",
    router=router,
    nav=NavItem(label="Documents", url="/documents",
                roles=None, icon="📄", order=16),  # between Parts (10) and Assemblies (20)
    migrations=MIGRATIONS,
    template_dir=Path(__file__).parent / "templates",
)
