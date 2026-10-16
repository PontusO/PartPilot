"""The one place stock quantities change.

`post_movement` applies a signed delta to a part's on-hand ON ONE SHELF, rolls the part's totals up
(`total_qty`/`total_alloc` = our shelves only, `customer_qty` = customer shelves) and writes a
`stock_movements` ledger row — atomically, inside the caller's transaction. Manual adjustments,
work-order issue/build, goods receiving and despatch all go through here so on-hand and the ledger
can never drift apart.

A shelf is a `stock_locations` row with a kind: ``ours`` (our stock) or ``customer`` (parts the
customer paid for up front; only their work orders draw from it). A movement never touches another
shelf's row: a customer build that lacks a part goes negative on its own shelf instead of silently
consuming Main.
"""

from __future__ import annotations

from ...core.db import Database

# Movement types. Sign convention is enforced by the caller via the delta; no logic branches
# on the string, so these are purely labels for the ledger/report (the delta carries direction).
RECEIVE = "RECEIVE"   # goods in (+)
ISSUE = "ISSUE"       # consumed by a build / shipped (-)
WOOSALE = "WOOSALE"   # sold via the WooCommerce webshop, applied by the stock sync (-)
BUILD = "BUILD"       # finished assembly into stock (+)
ADJUST = "ADJUST"     # manual correction (+/-)
OPENING = "OPENING"   # opening balance (+)


def default_shelf(conn) -> int:
    """Our default shelf: the lowest-id active 'ours' shelf ("Main"), created if none exists."""
    row = conn.execute(
        "SELECT id FROM stock_locations WHERE kind = 'ours' AND retired_at IS NULL ORDER BY id LIMIT 1"
    ).fetchone()
    if row:
        return row["id"]
    return conn.execute("INSERT INTO stock_locations (name, kind) VALUES ('Main', 'ours')").lastrowid


def _rollup(conn, part_id: int) -> None:
    """parts.total_qty = on-hand over OUR shelves; parts.customer_qty = over customer shelves."""
    conn.execute(
        """UPDATE parts SET
             total_qty = (SELECT COALESCE(SUM(pk.on_hand), 0) FROM part_stock pk
                          JOIN stock_locations l ON l.id = pk.location_id
                          WHERE pk.part_id = ? AND l.kind = 'ours'),
             customer_qty = (SELECT COALESCE(SUM(pk.on_hand), 0) FROM part_stock pk
                             JOIN stock_locations l ON l.id = pk.location_id
                             WHERE pk.part_id = ? AND l.kind = 'customer'),
             updated_at = datetime('now')
           WHERE id = ?""",
        (part_id, part_id, part_id),
    )


def post_movement(conn, part_id: int, *, delta: float, mtype: str, location_id: int | None,
                  reference: str | None = None, note: str | None = None,
                  user: str | None = None) -> float:
    """Apply ``delta`` on shelf ``location_id`` and record it. Runs inside the caller's
    transaction (no commit here). Returns the shelf's on-hand after the move.

    A negative result is allowed (oversell/backflush can drive a shelf negative); callers that
    must not go negative check availability first.
    """
    if not location_id:
        raise ValueError("A stock movement needs a shelf.")
    row = conn.execute(
        "SELECT id, on_hand FROM part_stock WHERE part_id = ? AND location_id = ? ORDER BY id LIMIT 1",
        (part_id, location_id),
    ).fetchone()
    if row is None:
        stock_id = conn.execute(
            "INSERT INTO part_stock (part_id, location_id, on_hand) VALUES (?, ?, 0)",
            (part_id, location_id),
        ).lastrowid
        old = 0.0
    else:
        stock_id, old = row["id"], row["on_hand"] or 0.0
    after = old + delta
    conn.execute("UPDATE part_stock SET on_hand = ? WHERE id = ?", (after, stock_id))
    _rollup(conn, part_id)
    conn.execute(
        "INSERT INTO stock_movements (part_id, location_id, mtype, qty_delta, qty_after, "
        "reference, note, user) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (part_id, location_id, mtype, delta, after, reference, note, user),
    )
    return after


def adjust_stock(db: Database, part_id: int, *, delta: float, mtype: str,
                 location_id: int | None = None, reference: str | None = None, note: str | None = None,
                 user: str | None = None) -> float:
    """Standalone single movement (opens + commits its own transaction). A missing shelf means
    our default shelf — this is the "our own stock" convenience path (manual adjust, webshop sync)."""
    with db.connect() as conn:
        after = post_movement(conn, part_id, delta=delta, mtype=mtype, reference=reference,
                              note=note, user=user, location_id=location_id or default_shelf(conn))
        conn.commit()
    return after


def shelf_on_hand(conn, part_id: int, location_id: int) -> float:
    row = conn.execute("SELECT on_hand FROM part_stock WHERE part_id = ? AND location_id = ?",
                       (part_id, location_id)).fetchone()
    return float(row["on_hand"] or 0) if row else 0.0


def free_on_shelf(conn, part_id: int, location_id: int) -> float:
    """On-hand minus allocated on one shelf (0 when the part has no row there)."""
    row = conn.execute("SELECT on_hand, allocated FROM part_stock WHERE part_id = ? AND location_id = ?",
                       (part_id, location_id)).fetchone()
    return float((row["on_hand"] or 0) - (row["allocated"] or 0)) if row else 0.0


def recompute_alloc(conn, part_id: int) -> None:
    """Mirror `allocations` onto part_stock.allocated per shelf and parts.total_alloc (ours only)."""
    conn.execute("UPDATE part_stock SET allocated = 0 WHERE part_id = ?", (part_id,))
    for r in conn.execute(
        "SELECT location_id, COALESCE(SUM(qty), 0) AS qty FROM allocations WHERE part_id = ? "
        "GROUP BY location_id", (part_id,),
    ).fetchall():
        loc = r["location_id"] or default_shelf(conn)
        cur = conn.execute("UPDATE part_stock SET allocated = ? WHERE part_id = ? AND location_id = ?",
                           (r["qty"], part_id, loc))
        if cur.rowcount == 0:
            conn.execute(
                "INSERT INTO part_stock (part_id, location_id, on_hand, allocated) VALUES (?, ?, 0, ?)",
                (part_id, loc, r["qty"]))
    conn.execute(
        """UPDATE parts SET total_alloc = (SELECT COALESCE(SUM(pk.allocated), 0) FROM part_stock pk
             JOIN stock_locations l ON l.id = pk.location_id WHERE pk.part_id = ? AND l.kind = 'ours')
           WHERE id = ?""", (part_id, part_id))


def movements_for_part(db: Database, part_id: int, limit: int = 50) -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT m.*, l.name AS location_name, l.kind AS location_kind
               FROM stock_movements m LEFT JOIN stock_locations l ON l.id = m.location_id
               WHERE m.part_id = ? ORDER BY m.id DESC LIMIT ?""",
            (part_id, limit),
        )]
