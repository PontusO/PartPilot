"""Catalog feature descriptor."""

from __future__ import annotations

from pathlib import Path

from ...core import Feature, NavItem
from .devmgmt_sync import devmgmt_sync_loop
from .migrations import MIGRATIONS
from .repo import home_stats
from .router import router

feature = Feature(
    name="catalog",
    router=router,
    nav=NavItem(label="Parts", url="/catalog", roles=None, icon="📦", order=10),
    migrations=MIGRATIONS,
    home_stats=home_stats,
    template_dir=Path(__file__).parent / "templates",
    background_tasks=(devmgmt_sync_loop,),   # drains the devmgmt push outbox
)
