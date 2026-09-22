"""The one place stock quantities change.

`post_movement` applies a signed delta to a part's on-hand at a location, rolls up the part's
`total_qty`, and writes a `stock_movements` ledger row — atomically, within a caller's
transaction. Manual adjustments (this feature), work-order issue/build, and goods receiving all
go through here so on-hand and the ledger can never drift apart.
"""

from __future__ import annotations

from ...core.db import Database
from .repo import _location_id

# Movement types. Sign convention is enforced by the caller via the delta; no logic branches
# on the string, so these are purely labels for the ledger/report (the delta carries direction).
RECEIVE = "RECEIVE"   # goods in (+)
ISSUE = "ISSUE"       # consumed by a build / shipped (-)
WOOSALE = "WOOSALE"   # sold via the WooCommerce webshop, applied by the stock sync (-)
BUILD = "BUILD"       # finished assembly into stock (+)
ADJUST = "ADJUST"     # manual correction (+/-)
OPENING = "OPENING"   # opening balance (+)


def post_movement(conn, part_id: int, *, delta: float, mtype: str, reference: str | None = None,
                  note: str | None = None, user: str | None = None,
                  location_id: int | None = None) -> float:
    """Apply ``delta`` to ``part_id``'s on-hand and record it. Runs inside the caller's
    transaction (no commit here). Returns the part's new total on-hand.

    A negative result is allowed (oversell/backflush can drive stock negative, as in miniMRP);
    callers that must not go negative should check availability first.
    """
    loc = _location_id(conn, location_id)
    row = conn.execute(
        "SELECT id, on_hand FROM part_stock WHERE part_id = ? AND location_id = ? ORDER BY id LIMIT 1",
        (part_id, loc),
    ).fetchone()
    if row is None:  # fall back to any existing stock row (e.g. legacy null-location rows)
        row = conn.execute(
            "SELECT id, on_hand FROM part_stock WHERE part_id = ? ORDER BY id LIMIT 1", (part_id,)
        ).fetchone()

    if row is None:
        stock_id = conn.execute(
            "INSERT INTO part_stock (part_id, location_id, on_hand) VALUES (?, ?, 0)", (part_id, loc)
        ).lastrowid
        old = 0.0
    else:
        stock_id, old = row["id"], row["on_hand"] or 0.0

    conn.execute("UPDATE part_stock SET on_hand = ? WHERE id = ?", (old + delta, stock_id))
    conn.execute(
        "UPDATE parts SET total_qty = "
        "(SELECT COALESCE(SUM(on_hand), 0) FROM part_stock WHERE part_id = ?), "
        "updated_at = datetime('now') WHERE id = ?",
        (part_id, part_id),
    )
    total_after = conn.execute("SELECT total_qty FROM parts WHERE id = ?", (part_id,)).fetchone()[0]
    conn.execute(
        "INSERT INTO stock_movements (part_id, location_id, mtype, qty_delta, qty_after, "
        "reference, note, user) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (part_id, loc, mtype, delta, total_after, reference, note, user),
    )
    return total_after


def adjust_stock(db: Database, part_id: int, *, delta: float, mtype: str,
                 reference: str | None = None, note: str | None = None,
                 user: str | None = None, location_id: int | None = None) -> float:
    """Standalone single movement (opens + commits its own transaction)."""
    with db.connect() as conn:
        total = post_movement(conn, part_id, delta=delta, mtype=mtype, reference=reference,
                              note=note, user=user, location_id=location_id)
        conn.commit()
    return total


def movements_for_part(db: Database, part_id: int, limit: int = 50) -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT m.*, l.name AS location_name
               FROM stock_movements m LEFT JOIN stock_locations l ON l.id = m.location_id
               WHERE m.part_id = ? ORDER BY m.id DESC LIMIT ?""",
            (part_id, limit),
        )]
