"""Planning calendar feature descriptor (no own tables; schedules work orders)."""

from __future__ import annotations

from pathlib import Path

from ...core import Feature, NavItem
from .router import router

feature = Feature(
    name="planning",
    router=router,
    nav=NavItem(label="Planning", url="/planning", roles=None, icon="📅", order=42),
    migrations=[],
    template_dir=Path(__file__).parent / "templates",
)
