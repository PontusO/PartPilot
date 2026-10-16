"""Merge the legacy catalog ``suppliers`` table into Contacts (kind='supplier').

History: miniMRP had one supplier table; PartPilot loaded it twice on 2026-06-11 (thin
``suppliers`` for procurement FKs, full ``contacts`` for the address book). Catalog migration 23
and purchase-orders migration 6 use these helpers to re-point every FK at ``contacts(id)``.
"""

from __future__ import annotations

import sqlite3

from ...core.db import rebuild_table, table_exists

SUPPLIER_ID_EXPR = "(SELECT s.contact_id FROM suppliers s WHERE s.id = supplier_id)"
FK_SWAP = {"REFERENCES suppliers(id)": "REFERENCES contacts(id)"}


def _has_column(conn, table, col) -> bool:
    return any(r[1] == col for r in conn.execute(f"PRAGMA table_info({table})"))


def still_on_old_fk(conn: sqlite3.Connection, table: str) -> bool:
    """True while ``table`` still declares ``REFERENCES suppliers(id)``. Each rebuild is skipped once
    its table points at contacts, so a migration interrupted between tables can simply be re-run
    without applying the id mapping a second time."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return bool(row) and "REFERENCES suppliers(id)" in row[0]


def assert_no_dangling(conn: sqlite3.Connection, table: str) -> None:
    """Every non-NULL supplier_id in ``table`` must name an existing suppliers row."""
    n = conn.execute(
        f"SELECT COUNT(*) FROM {table} t WHERE t.supplier_id IS NOT NULL "
        f"AND NOT EXISTS (SELECT 1 FROM suppliers s WHERE s.id = t.supplier_id)").fetchone()[0]
    if n:
        raise RuntimeError(
            f"{n} row(s) in {table} have a dangling supplier_id; fix them before migrating")


def map_suppliers_to_contacts(conn: sqlite3.Connection) -> None:
    """Give every ``suppliers`` row a ``contact_id``: same miniMRP id among contacts imported from
    miniMRP's supplier table (``source='sup'``; AddIDs collide across its address tables), else same
    trimmed case-insensitive name among supplier contacts (lowest id wins), else a new supplier
    contact. Idempotent: rows that already have a contact_id are left alone."""
    if not _has_column(conn, "suppliers", "contact_id"):
        conn.execute("ALTER TABLE suppliers ADD COLUMN contact_id INTEGER")
    conn.execute("""
        UPDATE suppliers SET contact_id = (
            SELECT c.id FROM contacts c WHERE c.kind = 'supplier' AND c.source = 'sup'
              AND c.minimrp_id IS NOT NULL AND c.minimrp_id = suppliers.minimrp_id
            ORDER BY c.id LIMIT 1)
        WHERE contact_id IS NULL""")
    conn.execute("""
        UPDATE suppliers SET contact_id = (
            SELECT c.id FROM contacts c WHERE c.kind = 'supplier'
              AND lower(trim(c.name)) = lower(trim(suppliers.name))
            ORDER BY c.id LIMIT 1)
        WHERE contact_id IS NULL""")
    for row in conn.execute(
            "SELECT id, name, short_name, url, currency, minimrp_id FROM suppliers "
            "WHERE contact_id IS NULL").fetchall():
        cid = conn.execute(
            """INSERT INTO contacts (kind, name, short_name, website, currency, minimrp_id, source)
               VALUES ('supplier', ?, ?, ?, ?, ?, 'sup-merge')""",
            ((row[1] or "").strip() or "?", row[2], row[3], row[4], row[5]),
        ).lastrowid
        conn.execute("UPDATE suppliers SET contact_id = ? WHERE id = ?", (cid, row[0]))
    conn.commit()


def migrate_part_suppliers(conn: sqlite3.Connection) -> None:
    """Catalog migration 23. On a fresh database (no contacts table yet) only the FK is swapped."""
    if not still_on_old_fk(conn, "part_suppliers"):
        return                                           # already rebuilt by an interrupted run
    if not table_exists(conn, "contacts"):
        rebuild_table(conn, "part_suppliers", sql_replacements=FK_SWAP, column_exprs={})
        return
    assert_no_dangling(conn, "part_suppliers")
    map_suppliers_to_contacts(conn)
    rebuild_table(conn, "part_suppliers", sql_replacements=FK_SWAP,
                  column_exprs={"supplier_id": SUPPLIER_ID_EXPR})
