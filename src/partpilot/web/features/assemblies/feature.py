"""Assemblies feature descriptor."""

from __future__ import annotations

from pathlib import Path

from ...core import Feature, NavItem
from .migrations import MIGRATIONS
from .router import router

feature = Feature(
    name="assemblies",
    router=router,
    nav=NavItem(label="Assemblies", url="/assemblies", roles=None, icon="🧩", order=20),
    migrations=MIGRATIONS,
    template_dir=Path(__file__).parent / "templates",
)
