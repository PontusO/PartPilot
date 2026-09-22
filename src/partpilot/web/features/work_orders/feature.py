"""Work orders feature descriptor."""

from __future__ import annotations

from pathlib import Path

from ...core import Feature, NavItem
from .migrations import MIGRATIONS
from .router import router

feature = Feature(
    name="work-orders",
    router=router,
    nav=NavItem(label="Work Orders", url="/work-orders", roles=None, icon="🏭", order=40),
    migrations=MIGRATIONS,
    template_dir=Path(__file__).parent / "templates",
)
