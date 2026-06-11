"""Customer orders feature descriptor."""

from __future__ import annotations

from pathlib import Path

from ...core import Feature, NavItem
from .migrations import MIGRATIONS
from .router import router

feature = Feature(
    name="customer-orders",
    router=router,
    nav=NavItem(label="Customer Orders", url="/customer-orders", roles=None, icon="📑", order=30),
    migrations=MIGRATIONS,
    template_dir=Path(__file__).parent / "templates",
)
