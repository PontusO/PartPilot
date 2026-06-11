"""Contacts feature descriptor."""

from __future__ import annotations

from pathlib import Path

from ...core import Feature, NavItem
from .migrations import MIGRATIONS
from .router import router

feature = Feature(
    name="contacts",
    router=router,
    nav=NavItem(label="Contacts", url="/contacts", roles=None, icon="📇", order=60),
    migrations=MIGRATIONS,
    template_dir=Path(__file__).parent / "templates",
)
