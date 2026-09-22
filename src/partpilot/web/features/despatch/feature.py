"""Despatch feature descriptor."""

from __future__ import annotations

from pathlib import Path

from ...core import Feature, NavItem
from .migrations import MIGRATIONS
from .router import router

feature = Feature(
    name="despatch",
    router=router,
    nav=NavItem(label="Despatch", url="/despatch", roles=None, icon="🚚", order=52),
    migrations=MIGRATIONS,
    template_dir=Path(__file__).parent / "templates",
)
