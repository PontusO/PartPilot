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
        rows = dict(conn.execute(
            "SELECT status, COUNT(*) FROM despatches GROUP BY status").fetchall())
    total = sum(rows.values())
    # 'packing' + 'packed' are pre-dispatch (still on the bench); 'open' is dispatched, awaiting invoice.
    return {"total": total, "packing": rows.get("packing", 0) + rows.get("packed", 0),
            "open": rows.get("open", 0), "invoiced": rows.get("invoiced", 0)}


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
            """SELECT l.id, l.qty, l.unit_price, l.part_id, l.packed, p.part_no, p.value
               FROM despatch_lines l LEFT JOIN parts p ON p.id = l.part_id
               WHERE l.despatch_id = ? ORDER BY l.id""",
            (despatch_id,),
        )]
    d = dict(head)
    for ln in lines:
        ln["line_total"] = (ln["qty"] or 0) * (ln["unit_price"] or 0)
        ln["packed"] = bool(ln["packed"])
    d["lines"] = lines
    d["total"] = sum(ln["line_total"] for ln in lines)
    d["packed_count"] = sum(1 for ln in lines if ln["packed"])
    d["all_packed"] = bool(lines) and all(ln["packed"] for ln in lines)
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
    """Order lines with something still to ship, with a suggested despatch qty. ``outstanding``
    already subtracts quantities sitting on open (packing/packed) packing lists, so opening a second
    list can never offer — or accept — more than remains to ship in total."""
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT l.id AS line_id, l.ordered_qty, l.shipped_qty, l.unit_price, l.discount_percent,
                      l.part_id, p.part_no, p.value, p.total_qty,
                      (SELECT COALESCE(SUM(a.qty), 0) FROM allocations a
                       WHERE a.customer_order_line_id = l.id) AS allocated,
                      (SELECT COALESCE(SUM(dl.qty), 0) FROM despatch_lines dl
                       JOIN despatches d ON d.id = dl.despatch_id
                       WHERE dl.order_line_id = l.id AND d.status IN ('packing', 'packed'))
                          AS open_packed
               FROM customer_order_lines l LEFT JOIN parts p ON p.id = l.part_id
               WHERE l.order_id = ? ORDER BY COALESCE(l.line_no, 1e9), l.id""",
            (order_id,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["outstanding"] = max(0.0, (d["ordered_qty"] or 0) - (d["shipped_qty"] or 0)
                               - (d["open_packed"] or 0))
        on_hand = d["total_qty"] or 0
        d["suggested_qty"] = min(d["outstanding"], on_hand) if on_hand > 0 else d["outstanding"]
        d["net_price"] = (d["unit_price"] or 0) * (1 - (d["discount_percent"] or 0) / 100.0)
        out.append(d)
    return out


# ---- create a packing list (no stock moves yet) ----

def create_packing_list(db: Database, order_id: int, selections: dict[int, float],
                        user: str | None = None) -> int | None:
    """Open a PACKING LIST for ``selections`` ({order_line_id: qty}) from a customer order.

    This records *what to pack* — it does NOT move stock or touch the order. The operator checks
    each line off (``set_packing``), confirms the package is ready (``confirm_packed``), and only
    then is it dispatched (``dispatch``), which is when stock actually ships. Returns the new
    despatch id (status 'packing'), or None if nothing was selected.
    """
    with db.connect() as conn:
        order = conn.execute("SELECT customer_id, status FROM customer_orders WHERE id = ?",
                             (order_id,)).fetchone()
        if order is None:
            raise ValueError("Customer order not found.")
        if order["status"] != "confirmed":
            raise ValueError(
                f"Only a confirmed order can be despatched (this one is {order['status']}).")
        lines = {r["id"]: r for r in conn.execute(
            "SELECT id, part_id, ordered_qty, shipped_qty, unit_price, discount_percent "
            "FROM customer_order_lines WHERE order_id = ?", (order_id,))}

        to_pack = {lid: q for lid, q in selections.items() if lid in lines and q and q > 0}
        if not to_pack:
            return None

        # Cap each line at what actually remains to ship: ordered − shipped − already on another
        # open packing list. Without this, two lists (double-click, or two operators) each carrying
        # the full outstanding qty would together over-ship the order.
        for lid, qty in to_pack.items():
            ln = lines[lid]
            open_packed = conn.execute(
                "SELECT COALESCE(SUM(dl.qty), 0) FROM despatch_lines dl "
                "JOIN despatches d ON d.id = dl.despatch_id "
                "WHERE dl.order_line_id = ? AND d.status IN ('packing', 'packed')", (lid,),
            ).fetchone()[0]
            remaining = (ln["ordered_qty"] or 0) - (ln["shipped_qty"] or 0) - (open_packed or 0)
            if qty > remaining + 1e-9:
                raise ValueError(
                    f"Line {lid}: packing {qty:g} exceeds the {max(remaining, 0):g} still to ship "
                    f"(ordered minus shipped and already-open packing lists).")

        desp_id = conn.execute(
            "INSERT INTO despatches (order_id, customer_id, status) VALUES (?, ?, 'packing')",
            (order_id, order["customer_id"]),
        ).lastrowid
        conn.execute("UPDATE despatches SET despatch_no = ? WHERE id = ?", (ref_no("DN", desp_id), desp_id))

        for line_id, qty in to_pack.items():
            ln = lines[line_id]
            net = (ln["unit_price"] or 0) * (1 - (ln["discount_percent"] or 0) / 100.0)
            conn.execute(
                "INSERT INTO despatch_lines (despatch_id, order_line_id, part_id, qty, unit_price) "
                "VALUES (?, ?, ?, ?, ?)",
                (desp_id, line_id, ln["part_id"], qty, net),
            )
        conn.commit()
    return desp_id


# ---- packing: check items off, then confirm ready to ship ----

def set_packing(db: Database, despatch_id: int, packed_line_ids: set[int]) -> None:
    """Persist which lines have been checked off the packing list. Only while still 'packing'."""
    with db.connect() as conn:
        row = conn.execute("SELECT status FROM despatches WHERE id = ?", (despatch_id,)).fetchone()
        if row is None:
            raise ValueError("Despatch not found.")
        if row["status"] != "packing":
            raise ValueError("This packing list is no longer being packed.")
        for ln in conn.execute("SELECT id FROM despatch_lines WHERE despatch_id = ?", (despatch_id,)):
            conn.execute("UPDATE despatch_lines SET packed = ? WHERE id = ?",
                         (1 if ln["id"] in packed_line_ids else 0, ln["id"]))
        conn.commit()


def confirm_packed(db: Database, despatch_id: int, user: str | None = None) -> None:
    """Mark the package ready to ship. Requires every line checked off. Status -> 'packed'.
    No stock has moved yet — dispatch is the next, separate step."""
    with db.connect() as conn:
        row = conn.execute("SELECT status FROM despatches WHERE id = ?", (despatch_id,)).fetchone()
        if row is None:
            raise ValueError("Despatch not found.")
        if row["status"] != "packing":
            raise ValueError(f"Only a packing list can be confirmed (this one is {row['status']}).")
        lines = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(packed), 0) AS packed FROM despatch_lines "
            "WHERE despatch_id = ?", (despatch_id,)).fetchone()
        if lines["n"] == 0 or lines["packed"] < lines["n"]:
            raise ValueError("Pack every item before confirming the package is ready.")
        conn.execute(
            "UPDATE despatches SET status = 'packed', packed_at = datetime('now'), packed_by = ?, "
            "updated_at = datetime('now') WHERE id = ?", (user, despatch_id))
        conn.commit()


def reopen_packing(db: Database, despatch_id: int) -> None:
    """Send a 'ready to ship' package back to packing (e.g. to add/remove an item). No stock moved."""
    with db.connect() as conn:
        row = conn.execute("SELECT status FROM despatches WHERE id = ?", (despatch_id,)).fetchone()
        if row is None:
            raise ValueError("Despatch not found.")
        if row["status"] != "packed":
            raise ValueError("Only a confirmed package can be reopened for packing.")
        conn.execute(
            "UPDATE despatches SET status = 'packing', packed_at = NULL, packed_by = NULL, "
            "updated_at = datetime('now') WHERE id = ?", (despatch_id,))
        conn.commit()


def cancel_packing(db: Database, despatch_id: int) -> int | None:
    """Discard a packing list before it ships (status packing/packed, no stock moved). Returns the
    order id it belonged to (for redirecting), or None if it wasn't found."""
    with db.connect() as conn:
        row = conn.execute("SELECT status, order_id FROM despatches WHERE id = ?",
                           (despatch_id,)).fetchone()
        if row is None:
            return None
        if row["status"] not in ("packing", "packed"):
            raise ValueError("Only a packing list that hasn't shipped can be cancelled.")
        conn.execute("DELETE FROM despatches WHERE id = ?", (despatch_id,))  # lines cascade
        conn.commit()
        return row["order_id"]


# ---- dispatch: ship the confirmed package (this is where stock moves) ----

def dispatch(db: Database, despatch_id: int, user: str | None = None) -> None:
    """Ship a confirmed package. Posts ISSUE movements, consumes allocation, bumps shipped_qty,
    stamps the despatch date, flips the order to 'shipped' when fully shipped, and marks the
    despatch 'open' (despatched, awaiting invoice). Requires status 'packed'."""
    with db.connect() as conn:
        d = conn.execute("SELECT id, order_id, despatch_no, status FROM despatches WHERE id = ?",
                         (despatch_id,)).fetchone()
        if d is None:
            raise ValueError("Despatch not found.")
        if d["status"] != "packed":
            raise ValueError(f"Confirm the package is ready before dispatching (this one is {d['status']}).")
        desp_ref = d["despatch_no"] or ref_no("DN", despatch_id)
        order_id = d["order_id"]

        # Re-validate against the order lines at ship time — a second packed list (or an order edit)
        # since packing must not push shipped_qty past ordered_qty.
        for ln in conn.execute(
            """SELECT dl.qty, ol.ordered_qty, ol.shipped_qty FROM despatch_lines dl
               JOIN customer_order_lines ol ON ol.id = dl.order_line_id
               WHERE dl.despatch_id = ?""", (despatch_id,),
        ):
            if ln["qty"] > (ln["ordered_qty"] or 0) - (ln["shipped_qty"] or 0) + 1e-9:
                raise ValueError(
                    "Dispatching this package would ship more than the order's remaining quantity — "
                    "another despatch has shipped in the meantime. Reopen and adjust the packing list.")

        for ln in conn.execute(
            "SELECT id, order_line_id, part_id, qty FROM despatch_lines WHERE despatch_id = ?",
            (despatch_id,),
        ).fetchall():
            if ln["part_id"]:
                stock.post_movement(conn, ln["part_id"], delta=-ln["qty"], mtype=stock.ISSUE,
                                    reference=desp_ref, note="despatch", user=user)
                if ln["order_line_id"]:
                    _consume_allocation(conn, ln["order_line_id"], ln["qty"])
            if ln["order_line_id"]:
                conn.execute("UPDATE customer_order_lines SET shipped_qty = shipped_qty + ? WHERE id = ?",
                             (ln["qty"], ln["order_line_id"]))

        conn.execute(
            "UPDATE despatches SET status = 'open', despatch_date = date('now'), "
            "updated_at = datetime('now') WHERE id = ?", (despatch_id,))

        if order_id is not None:
            outstanding = conn.execute(
                "SELECT COALESCE(SUM(ordered_qty - shipped_qty), 0) FROM customer_order_lines "
                "WHERE order_id = ?", (order_id,)).fetchone()[0]
            if outstanding <= 0:
                conn.execute("UPDATE customer_orders SET status = 'shipped', updated_at = datetime('now') "
                             "WHERE id = ? AND status != 'cancelled'", (order_id,))
        conn.commit()


def mark_invoiced(db: Database, despatch_id: int, invoice_no: str | None, invoice_date: str | None) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE despatches SET status = 'invoiced', invoice_no = ?, "
            "invoice_date = COALESCE(?, date('now')), updated_at = datetime('now') WHERE id = ?",
            (invoice_no, invoice_date, despatch_id),
        )
        # Close the loop: once every despatch of a fully-shipped order is invoiced, the order is
        # complete. This is the only writer of 'complete' — it drops the order out of the open /
        # backlog buckets that would otherwise hold it forever.
        row = conn.execute("SELECT order_id FROM despatches WHERE id = ?", (despatch_id,)).fetchone()
        if row and row["order_id"] is not None:
            open_desp = conn.execute(
                "SELECT COUNT(*) FROM despatches WHERE order_id = ? AND status != 'invoiced'",
                (row["order_id"],)).fetchone()[0]
            if open_desp == 0:
                conn.execute(
                    "UPDATE customer_orders SET status = 'complete', updated_at = datetime('now') "
                    "WHERE id = ? AND status = 'shipped'", (row["order_id"],))
        conn.commit()
