"""Reports feature descriptor.

Reports are **read-only views over data other features write** — this module owns no tables
and ships no migrations. The first report is the stock-movement ledger (catalog's
``stock_movements``); more reports become extra routes + a card on the index page.
"""

from __future__ import annotations

from pathlib import Path

from ...core import Feature, NavItem
from .router import router

feature = Feature(
    name="reports",
    router=router,
    nav=NavItem(label="Reports", url="/reports", roles=None, icon="📊", order=70),
    migrations=[],
    template_dir=Path(__file__).parent / "templates",
)
