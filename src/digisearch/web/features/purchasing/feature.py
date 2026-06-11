"""Purchasing feature descriptor — what this module contributes to the platform."""

from __future__ import annotations

from pathlib import Path

from ...auth import PURCHASE_ROLES
from ...core import Feature, NavItem
from .router import router

feature = Feature(
    name="purchasing",
    router=router,
    nav=NavItem(label="Purchasing", url="/purchasing", roles=frozenset(PURCHASE_ROLES),
                icon="🛒", order=50),
    template_dir=Path(__file__).parent / "templates",
)
