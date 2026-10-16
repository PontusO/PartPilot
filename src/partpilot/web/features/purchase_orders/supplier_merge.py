"""Purchase-orders migration 6: purchase_orders/goods_receipts.supplier_id -> contacts(id), then
drop the legacy ``suppliers`` table. Runs after catalog 23 and the contacts migrations."""

from __future__ import annotations

import sqlite3

from ...core.db import rebuild_table, table_exists
from ..catalog.supplier_merge import (FK_SWAP, SUPPLIER_ID_EXPR, assert_no_dangling,
                                      map_suppliers_to_contacts)

_TABLES = ("purchase_orders", "goods_receipts")


def migrate_purchase_orders_and_grns(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "suppliers"):
        return                                           # already merged
    has_rows = conn.execute("SELECT 1 FROM suppliers LIMIT 1").fetchone() is not None
    if has_rows and table_exists(conn, "contacts"):
        for t in _TABLES:
            assert_no_dangling(conn, t)
        map_suppliers_to_contacts(conn)                  # idempotent; catalog 23 normally did this
        exprs = {"supplier_id": SUPPLIER_ID_EXPR}
    else:
        exprs = {}                                       # fresh database: nothing to map
    for t in _TABLES:
        rebuild_table(conn, t, sql_replacements=FK_SWAP, column_exprs=exprs)
    unmapped = conn.execute(
        "SELECT COUNT(*) FROM suppliers WHERE contact_id IS NULL").fetchone()[0] if has_rows else 0
    if unmapped:
        raise RuntimeError(f"{unmapped} supplier row(s) could not be mapped to a contact")
    conn.execute("DROP TABLE suppliers")
    conn.commit()
