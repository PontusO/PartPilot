"""Read-only queries for the Reports feature.

Reports own no tables — they slice what other features write. The stock-movement ledger lives
in catalog's ``stock_movements`` (every quantity change posts a row via
``catalog.stock.post_movement``); here we just filter it by date range / movement type for
browsing. ``moved_at`` is stored as ``YYYY-MM-DD HH:MM:SS``, so ``date(moved_at)`` gives a clean
day key that the ``YYYY-MM-DD`` pickers compare against (both ends inclusive).
"""

from __future__ import annotations

from ...core.db import Database

# Movement types as written by catalog.stock — kept here so Reports stays self-contained.
MOVEMENT_TYPES = ["RECEIVE", "ISSUE", "WOOSALE", "BUILD", "ADJUST", "OPENING"]


def _filter(start, end, mtype):
    """Build a shared WHERE clause + params for both the listing and the summary."""
    where, params = [], []
    if start:
        where.append("date(m.moved_at) >= ?")
        params.append(start)
    if end:
        where.append("date(m.moved_at) <= ?")
        params.append(end)
    if mtype:
        where.append("m.mtype = ?")
        params.append(mtype)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    return clause, params


def stock_movements(db: Database, *, start=None, end=None, mtype=None, limit=2000) -> list[dict]:
    """Ledger rows (newest first), joined to the part and location, capped at ``limit``."""
    clause, params = _filter(start, end, mtype)
    sql = f"""
        SELECT m.id, m.moved_at, m.mtype, m.qty_delta, m.qty_after,
               m.reference, m.note, m.user,
               p.id AS part_id, p.part_no, p.value,
               l.name AS location_name
        FROM stock_movements m
        JOIN parts p ON p.id = m.part_id
        LEFT JOIN stock_locations l ON l.id = m.location_id
        {clause}
        ORDER BY m.id DESC
        LIMIT ?
    """
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sql, [*params, limit])]


def stock_movement_summary(db: Database, *, start=None, end=None, mtype=None) -> dict:
    """Totals for the selected range: movement count, units in/out, distinct parts touched."""
    clause, params = _filter(start, end, mtype)
    sql = f"""
        SELECT COUNT(*) AS moves,
               COUNT(DISTINCT m.part_id) AS parts,
               COALESCE(SUM(CASE WHEN m.qty_delta > 0 THEN m.qty_delta END), 0) AS total_in,
               COALESCE(SUM(CASE WHEN m.qty_delta < 0 THEN -m.qty_delta END), 0) AS total_out
        FROM stock_movements m
        {clause}
    """
    with db.connect() as conn:
        return dict(conn.execute(sql, params).fetchone())
