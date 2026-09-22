"""Setup & Tools feature descriptor (admin-only)."""

from __future__ import annotations

from pathlib import Path

from ...core import Feature, NavItem
from .migrations import MIGRATIONS
from .router import SETUP_ROLES, router
from .scheduler import webshop_sync_loop

feature = Feature(
    name="setup",
    router=router,
    nav=NavItem(label="Setup & Tools", url="/setup", roles=frozenset(SETUP_ROLES),
                icon="⚙️", order=80),
    migrations=MIGRATIONS,
    template_dir=Path(__file__).parent / "templates",
    background_tasks=(webshop_sync_loop,),   # daily webshop pull at the configured time
)
