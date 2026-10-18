"""Article Register feature descriptor."""

from __future__ import annotations

from pathlib import Path

from ...core import Feature, NavItem
from .migrations import MIGRATIONS
from .repo import home_stats
from .router import router

feature = Feature(
    name="article_register",
    router=router,
    nav=NavItem(label="Article Register", url="/article-register",
                roles=None, icon="🔖", order=62),  # support function: just below Contacts (60), above Reports (70)
    migrations=MIGRATIONS,
    home_stats=home_stats,
    template_dir=Path(__file__).parent / "templates",
)
