"""Help feature descriptor — the user manual. Owns no tables and ships no migrations."""

from __future__ import annotations

from pathlib import Path

from ...core import Feature, NavItem
from .router import router

feature = Feature(
    name="help",
    router=router,
    nav=NavItem(label="Manual", url="/help", roles=None, icon="📖", order=75),  # between Reports (70) and Setup (80)
    migrations=[],
    template_dir=Path(__file__).parent / "templates",
)
