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
                roles=None, icon="📄", order=16),  # right after Article Register (15)
    migrations=MIGRATIONS,
    template_dir=Path(__file__).parent / "templates",
)
