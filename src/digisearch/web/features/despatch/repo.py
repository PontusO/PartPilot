"""Despatch queries + the ship/invoice actions.

Self-contained SQL over customer_orders/_lines, allocations and parts (no customer_orders import,
to keep the module graph acyclic). Shipping posts ISSUE movements via catalog.stock.
"""

from __future__ import annotations

from ...core import ref_no
from ...core.db import Database
from ..catalog import stock


def _recompute_part_alloc(conn, part_id: int) -> None:
    total = conn.execute(
        "SELECT COALESCE(SUM(qty), 0) FROM allocations WHERE part_id = ?", (part_id,)
    ).fetchone()[0]
    conn.execute("UPDATE parts SET total_alloc = ? WHERE id = ?", (total, part_id))
    row = conn.execute("SELECT id FROM part_stock WHERE part_id = ? ORDER BY id LIMIT 1",
                       (part_id,)).fetchone()
    if row:
        conn.execute("UPDATE part_stock SET allocated = ? WHERE id = ?", (total, row["id"]))


def _consume_allocation(conn, order_line_id: int, qty: float) -> None:
    """Release up to ``qty`` of stock reserved to an order line (it's being shipped)."""
    remaining = qty
    for a in conn.execute(
        "SELECT id, part_id, qty FROM allocations WHERE customer_order_line_id = ? ORDER BY id",
        (order_line_id,),
    ).fetchall():
        if remaining <= 0:
            break
        take = min(a["qty"], remaining)
        if take >= a["qty"]:
            conn.execute("DELETE FROM allocations WHERE id = ?", (a["id"],))
        else:
            conn.execute("UPDATE allocations SET qty = qty - ? WHERE id = ?", (take, a["id"]))
        _recompute_part_alloc(conn, a["part_id"])
        remaining -= take


# ---- reads ----

def summary(db: Database) -> dict:
    with db.connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM despatches").fetchone()[0]
        open_n = conn.execute("SELECT COUNT(*) FROM despatches WHERE status = 'open'").fetchone()[0]
    return {"total": total, "open": open_n, "invoiced": total - open_n}


def list_despatches(db: Database, search: str | None = None) -> list[dict]:
    like = f"%{search}%" if search else None
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT d.id, d.despatch_no, d.despatch_date, d.status, d.invoice_no,
                      c.name AS customer_name, o.order_ref,
                      (SELECT COUNT(*) FROM despatch_lines l WHERE l.despatch_id = d.id) AS line_count
               FROM despatches d
               LEFT JOIN contacts c ON c.id = d.customer_id
               LEFT JOIN customer_orders o ON o.id = d.order_id
               WHERE (:s IS NULL OR d.despatch_no LIKE :like OR c.name LIKE :like OR o.order_ref LIKE :like)
               ORDER BY d.id DESC""",
            {"s": search, "like": like},
        )]


def get_despatch(db: Database, despatch_id: int) -> dict | None:
    with db.connect() as conn:
        head = conn.execute(
            """SELECT d.*, c.name AS customer_name, o.order_ref
               FROM despatches d LEFT JOIN contacts c ON c.id = d.customer_id
               LEFT JOIN customer_orders o ON o.id = d.order_id WHERE d.id = ?""",
            (despatch_id,),
        ).fetchone()
        if head is None:
            return None
        lines = [dict(r) for r in conn.execute(
            """SELECT l.id, l.qty, l.unit_price, l.part_id, p.part_no, p.value
               FROM despatch_lines l LEFT JOIN parts p ON p.id = l.part_id
               WHERE l.despatch_id = ? ORDER BY l.id""",
            (despatch_id,),
        )]
    d = dict(head)
    for ln in lines:
        ln["line_total"] = (ln["qty"] or 0) * (ln["unit_price"] or 0)
    d["lines"] = lines
    d["total"] = sum(ln["line_total"] for ln in lines)
    return d


def despatches_for_order(db: Database, order_id: int) -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, despatch_no, despatch_date, status, invoice_no FROM despatches "
            "WHERE order_id = ? ORDER BY id", (order_id,))]


def order_header(db: Database, order_id: int) -> dict | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT o.id, o.order_ref, o.status, o.customer_id, c.name AS customer_name "
            "FROM customer_orders o LEFT JOIN contacts c ON c.id = o.customer_id WHERE o.id = ?",
            (order_id,),
        ).fetchone()
        if row is None:
            return None
        header = dict(row)
        da = conn.execute(
            """SELECT a.* FROM contact_addresses a
               JOIN customer_orders o ON o.delivery_address_id = a.id WHERE o.id = ?""",
            (order_id,),
        ).fetchone()
    header["delivery_address"] = dict(da) if da else None
    return header


def shippable_lines(db: Database, order_id: int) -> list[dict]:
    """Order lines with something still to ship, with a suggested despatch qty."""
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT l.id AS line_id, l.ordered_qty, l.shipped_qty, l.unit_price, l.discount_percent,
                      l.part_id, p.part_no, p.value, p.total_qty,
                      (SELECT COALESCE(SUM(a.qty), 0) FROM allocations a
                       WHERE a.customer_order_line_id = l.id) AS allocated
               FROM customer_order_lines l LEFT JOIN parts p ON p.id = l.part_id
               WHERE l.order_id = ? ORDER BY COALESCE(l.line_no, 1e9), l.id""",
            (order_id,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["outstanding"] = max(0.0, (d["ordered_qty"] or 0) - (d["shipped_qty"] or 0))
        on_hand = d["total_qty"] or 0
        d["suggested_qty"] = min(d["outstanding"], on_hand) if on_hand > 0 else d["outstanding"]
        d["net_price"] = (d["unit_price"] or 0) * (1 - (d["discount_percent"] or 0) / 100.0)
        out.append(d)
    return out


# ---- create a despatch (ship) ----

def create_despatch(db: Database, order_id: int, selections: dict[int, float],
                    user: str | None = None) -> int | None:
    """Ship ``selections`` ({order_line_id: qty}) from a customer order. Posts ISSUE movements,
    consumes allocation, bumps shipped_qty, and flips the order to 'shipped' when fully shipped.
    Returns the new despatch id, or None if nothing was shipped."""
    with db.connect() as conn:
        order = conn.execute("SELECT customer_id FROM customer_orders WHERE id = ?", (order_id,)).fetchone()
        if order is None:
            raise ValueError("Customer order not found.")
        lines = {r["id"]: r for r in conn.execute(
            "SELECT id, part_id, ordered_qty, shipped_qty, unit_price, discount_percent "
            "FROM customer_order_lines WHERE order_id = ?", (order_id,))}

        to_ship = {lid: q for lid, q in selections.items() if lid in lines and q and q > 0}
        if not to_ship:
            return None

        desp_id = conn.execute(
            "INSERT INTO despatches (order_id, customer_id, despatch_date, status) "
            "VALUES (?, ?, date('now'), 'open')",
            (order_id, order["customer_id"]),
        ).lastrowid
        desp_ref = ref_no("DN", desp_id)
        conn.execute("UPDATE despatches SET despatch_no = ? WHERE id = ?", (desp_ref, desp_id))

        for line_id, qty in to_ship.items():
            ln = lines[line_id]
            net = (ln["unit_price"] or 0) * (1 - (ln["discount_percent"] or 0) / 100.0)
            if ln["part_id"]:
                stock.post_movement(conn, ln["part_id"], delta=-qty, mtype=stock.ISSUE,
                                    reference=desp_ref, note="despatch", user=user)
                _consume_allocation(conn, line_id, qty)
            conn.execute("UPDATE customer_order_lines SET shipped_qty = shipped_qty + ? WHERE id = ?",
                         (qty, line_id))
            conn.execute(
                "INSERT INTO despatch_lines (despatch_id, order_line_id, part_id, qty, unit_price) "
                "VALUES (?, ?, ?, ?, ?)",
                (desp_id, line_id, ln["part_id"], qty, net),
            )

        outstanding = conn.execute(
            "SELECT COALESCE(SUM(ordered_qty - shipped_qty), 0) FROM customer_order_lines WHERE order_id = ?",
            (order_id,),
        ).fetchone()[0]
        if outstanding <= 0:
            conn.execute("UPDATE customer_orders SET status = 'shipped', updated_at = datetime('now') "
                         "WHERE id = ? AND status != 'cancelled'", (order_id,))
        conn.commit()
    return desp_id


def mark_invoiced(db: Database, despatch_id: int, invoice_no: str | None, invoice_date: str | None) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE despatches SET status = 'invoiced', invoice_no = ?, "
            "invoice_date = COALESCE(?, date('now')), updated_at = datetime('now') WHERE id = ?",
            (invoice_no, invoice_date, despatch_id),
        )
        conn.commit()
