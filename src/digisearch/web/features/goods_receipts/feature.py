"""Goods Receipts feature descriptor (view-only; tables owned by purchase_orders)."""

from __future__ import annotations

from pathlib import Path

from ...core import Feature, NavItem
from .router import router

feature = Feature(
    name="goods-receipts",
    router=router,
    nav=NavItem(label="Goods Receipts", url="/goods-receipts", roles=None, icon="📦", order=46),
    migrations=[],
    template_dir=Path(__file__).parent / "templates",
)
