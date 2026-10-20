"""Documents feature descriptor."""

from __future__ import annotations

from pathlib import Path

from ...core import Feature, NavItem
from .migrations import MIGRATIONS
from .repo import home_stats
from .router import router

feature = Feature(
    name="documents",
    router=router,
    nav=NavItem(label="Documents", url="/documents",
                roles=None, icon="📄", order=25),  # between Assemblies (20) and Customer Orders (30)
    migrations=MIGRATIONS,
    home_stats=home_stats,
    template_dir=Path(__file__).parent / "templates",
)
