"""Queries + writes for customer orders (header + lines, with totals).

Customers come from the contacts address book (kind='customer'); products are catalog parts.
Totals mirror miniMRP: per-line net = price x (1 - discount%), order subtotal, then an
order-level discount, delivery charge and tax rate on top.
"""

from __future__ import annotations

import re

from ...core import ref_no
from ...core.db import Database
from ..catalog import pricing
from ..setup import repo as setup_repo
from . import export

STATUSES = ("draft", "confirmed", "shipped", "complete", "cancelled")
OPEN_STATUSES = ("draft", "confirmed", "shipped")
# An acknowledgement may be (re)issued while the order is still open and unshipped.
ACK_STATUSES = ("draft", "confirmed")

# Header columns written from the order form (everything except id/timestamps/minimrp_id).
_ORDER_FIELDS = ("order_ref", "customer_id", "customer_po", "status", "order_date",
                 "required_date", "currency", "discount_rate", "delivery_charge",
                 "tax_rate", "notes", "delivery_address_id", "invoice_address_id")


def summary(db: Database) -> dict:
    with db.connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM customer_orders").fetchone()[0]
        open_n = conn.execute(
            "SELECT COUNT(*) FROM customer_orders "
            "WHERE status NOT IN ('complete', 'cancelled')"
        ).fetchone()[0]
        backlog = conn.execute(
            """SELECT COALESCE(SUM(l.ordered_qty * COALESCE(l.unit_price, 0)
                      * (1 - COALESCE(l.discount_percent, 0) / 100.0)), 0)
               FROM customer_order_lines l JOIN customer_orders o ON o.id = l.order_id
               WHERE o.status NOT IN ('complete', 'cancelled')"""
        ).fetchone()[0]
    return {"total": total, "open": open_n, "backlog": backlog}


def list_orders(db: Database, status: str | None = None, search: str | None = None) -> list[dict]:
    like = f"%{search}%" if search else None
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT o.id, o.order_ref, o.customer_po, o.status, o.order_date, o.required_date,
                      o.currency, c.name AS customer_name,
                      (SELECT COUNT(*) FROM customer_order_lines l WHERE l.order_id = o.id) AS line_count,
                      (SELECT COALESCE(SUM(l.ordered_qty * COALESCE(l.unit_price, 0)
                              * (1 - COALESCE(l.discount_percent, 0) / 100.0)), 0)
                       FROM customer_order_lines l WHERE l.order_id = o.id) AS subtotal
               FROM customer_orders o LEFT JOIN contacts c ON c.id = o.customer_id
               WHERE (:status IS NULL OR o.status = :status)
                 AND (:search IS NULL OR o.order_ref LIKE :like OR o.customer_po LIKE :like
                      OR c.name LIKE :like)
               ORDER BY COALESCE(o.order_date, '') DESC, o.id DESC""",
            {"status": status, "search": search, "like": like},
        )]


def customers(db: Database) -> list[dict]:
    """Customer contacts available to place an order against."""
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, name, currency FROM contacts WHERE kind = 'customer' ORDER BY name"
        )]


def _all_prices(conn) -> dict[int, float | None]:
    """part_id -> price: a component's own ``unit_cost``, or an assembly's rolled-up BOM cost
    (sum of child qty x price, recursively). One pass over parts + bom_lines; cycle-guarded."""
    parts = {r["id"]: (r["kind"], r["unit_cost"])
             for r in conn.execute("SELECT id, kind, unit_cost FROM parts")}
    children: dict[int, list] = {}
    for r in conn.execute("SELECT parent_id, child_id, qty_per FROM bom_lines"):
        children.setdefault(r["parent_id"], []).append((r["child_id"], r["qty_per"]))
    memo: dict[int, float | None] = {}

    def price(pid: int, seen: frozenset) -> float | None:
        kind, unit_cost = parts.get(pid, (None, None))
        if kind != "ASSY":
            return unit_cost
        if pid in memo:
            return memo[pid]
        if pid in seen:          # cyclic BOM guard
            return 0.0
        total = sum((price(cid, seen | {pid}) or 0.0) * (qty or 0)
                    for cid, qty in children.get(pid, []))
        memo[pid] = total
        return total

    return {pid: price(pid, frozenset()) for pid in parts}


def _all_loaded_prices(conn, overhead: float) -> dict[int, float | None]:
    """part_id -> its LOADED cost at qty 1 (material × overhead, rolled up the BOM) — what an order
    line for that part defaults to. Memoised in one pass like ``_all_prices`` (leaf loaded via
    ``pricing.leaf_sell_unit``). No manufacturing margin: production cost + profit are priced outside
    PartPilot as a per-product 97- part."""
    kinds = {r["id"]: r["kind"] for r in conn.execute("SELECT id, kind FROM parts")}
    children: dict[int, list] = {}
    for r in conn.execute("SELECT parent_id, child_id, qty_per FROM bom_lines"):
        children.setdefault(r["parent_id"], []).append((r["child_id"], r["qty_per"]))
    memo: dict[int, float | None] = {}

    def loaded(pid: int, seen: frozenset) -> float | None:
        kind = kinds.get(pid)
        if kind is None:
            return None
        if kind != "ASSY":
            return pricing.leaf_sell_unit(conn, pid, 1, overhead)
        if pid in memo:
            return memo[pid]
        if pid in seen:          # cyclic BOM guard
            return 0.0
        total = sum((loaded(cid, seen | {pid}) or 0.0) * (qty or 0)
                    for cid, qty in children.get(pid, []))
        memo[pid] = total
        return total

    return {pid: loaded(pid, frozenset()) for pid in kinds}


def parts_for_picker(db: Database) -> list[dict]:
    """Every part/assembly that can be added to an order, each with its computed ``price`` — the
    LOADED cost at qty 1 (what the order line defaults to) — used as the pick hint."""
    overhead = setup_repo.get_default_markup(db)
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, part_no, value, kind, total_qty, unit_cost FROM parts ORDER BY part_no"
        )]
        prices = _all_loaded_prices(conn, overhead)
    for r in rows:
        r["price"] = prices.get(r["id"])
    return rows


def _with_totals(order: dict, lines: list[dict]) -> dict:
    subtotal = 0.0
    for ln in lines:
        price = ln["unit_price"] or 0.0
        disc = ln["discount_percent"] or 0.0
        ln["net_price"] = price * (1 - disc / 100.0)
        ln["line_total"] = (ln["ordered_qty"] or 0.0) * ln["net_price"]
        subtotal += ln["line_total"]
    disc_amount = subtotal * (order.get("discount_rate") or 0.0) / 100.0
    net_goods = subtotal - disc_amount
    delivery = order.get("delivery_charge") or 0.0
    tax = (net_goods + delivery) * (order.get("tax_rate") or 0.0) / 100.0
    order["lines"] = lines
    order["subtotal"] = subtotal
    order["discount_amount"] = disc_amount
    order["delivery"] = delivery
    order["tax"] = tax
    order["grand_total"] = net_goods + delivery + tax
    return order


def get_order(db: Database, order_id: int) -> dict | None:
    overhead = setup_repo.get_default_markup(db)
    with db.connect() as conn:
        head = conn.execute(
            """SELECT o.*, c.name AS customer_name, c.short_name AS customer_short,
                      c.contact AS customer_contact, c.email AS customer_email,
                      c.phone AS customer_phone, c.phone2 AS customer_phone2,
                      c.fax AS customer_fax, c.address AS customer_address,
                      c.postcode AS customer_postcode, c.country AS customer_country,
                      c.website AS customer_website
               FROM customer_orders o LEFT JOIN contacts c ON c.id = o.customer_id
               WHERE o.id = ?""",
            (order_id,),
        ).fetchone()
        if head is None:
            return None
        lines = [dict(r) for r in conn.execute(
            """SELECT l.id, l.line_no, l.ordered_qty, l.unit_price, l.discount_percent,
                      l.shipped_qty, l.notes, l.price_overridden,
                      p.id AS part_id, p.part_no, p.value, p.kind, p.unit_cost,
                      (SELECT COALESCE(SUM(a.qty), 0) FROM allocations a
                       WHERE a.customer_order_line_id = l.id) AS allocated
               FROM customer_order_lines l LEFT JOIN parts p ON p.id = l.part_id
               WHERE l.order_id = ?
               ORDER BY COALESCE(l.line_no, 1e9), l.id""",
            (order_id,),
        )]
        # The auto (tiered) sell price is only shown for non-overridden lines (as the "auto" price
        # hint). Skip the (recursive BOM) rollup for overridden lines, and memoise by (part, qty) so
        # repeated products cost one rollup, not one per line.
        sell_cache: dict = {}
        for ln in lines:
            ln["price_overridden"] = bool(ln["price_overridden"])
            if ln["price_overridden"] or not ln["part_id"]:
                ln["sell_price"] = None
                continue
            key = (ln["part_id"], ln["ordered_qty"] or 1)
            if key not in sell_cache:
                sell_cache[key] = _loaded_price_at(conn, ln["part_id"], ln["ordered_qty"] or 1,
                                                   overhead)
            ln["sell_price"] = sell_cache[key]
        delivery = invoice = None
        if head["delivery_address_id"]:
            r = conn.execute("SELECT * FROM contact_addresses WHERE id = ?",
                             (head["delivery_address_id"],)).fetchone()
            delivery = dict(r) if r else None
        if head["invoice_address_id"]:
            r = conn.execute("SELECT * FROM contact_addresses WHERE id = ?",
                             (head["invoice_address_id"],)).fetchone()
            invoice = dict(r) if r else None

    order = _with_totals(dict(head), lines)
    order["customer"] = {"name": order.get("customer_name"), "contact": order.get("customer_contact"),
                         "email": order.get("customer_email"), "phone": order.get("customer_phone"),
                         "phone2": order.get("customer_phone2"), "fax": order.get("customer_fax"),
                         "address": order.get("customer_address"),
                         "postcode": order.get("customer_postcode"),
                         "country": order.get("customer_country"),
                         "website": order.get("customer_website")}
    # The chosen structured addresses (None → callers fall back to the base customer address).
    order["delivery_address"] = delivery
    order["invoice_address"] = invoice
    return order


def _default_address_id(conn, customer_id: int | None, usage_col: str, default_col: str) -> int | None:
    if not customer_id:
        return None
    row = conn.execute(
        f"SELECT id FROM contact_addresses WHERE contact_id = ? AND {usage_col} = 1 "
        f"ORDER BY {default_col} DESC, id LIMIT 1", (customer_id,)).fetchone()
    return row["id"] if row else None


def create_order(db: Database, data: dict) -> int:
    data = {**data, "status": data.get("status") or "draft"}
    cols = ", ".join(_ORDER_FIELDS)
    placeholders = ", ".join("?" for _ in _ORDER_FIELDS)
    with db.connect() as conn:
        # Pre-fill the delivery/invoice address from the customer's defaults when not supplied.
        if data.get("delivery_address_id") is None:
            data["delivery_address_id"] = _default_address_id(
                conn, data.get("customer_id"), "is_delivery", "is_default_delivery")
        if data.get("invoice_address_id") is None:
            data["invoice_address_id"] = _default_address_id(
                conn, data.get("customer_id"), "is_invoice", "is_default_invoice")
        order_id = conn.execute(
            f"INSERT INTO customer_orders ({cols}) VALUES ({placeholders})",
            tuple(data.get(f) for f in _ORDER_FIELDS),
        ).lastrowid
        ref = (data.get("order_ref") or "").strip()
        if not ref or re.fullmatch(r"CO-\d+", ref):   # blank or an auto-style ref → bind to the real id
            conn.execute("UPDATE customer_orders SET order_ref = ? WHERE id = ?",
                         (ref_no("CO", order_id), order_id))
        conn.commit()
    return order_id


def next_order_ref(db: Database) -> str:
    """The CO number a new order would get — to pre-fill the form (re-derived to the real id on save)."""
    with db.connect() as conn:
        nxt = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM customer_orders").fetchone()[0]
    return ref_no("CO", nxt)


def update_order(db: Database, order_id: int, data: dict) -> None:
    assignments = ", ".join(f"{f} = ?" for f in _ORDER_FIELDS)
    with db.connect() as conn:
        conn.execute(
            f"UPDATE customer_orders SET {assignments}, updated_at = datetime('now') WHERE id = ?",
            (*[data.get(f) for f in _ORDER_FIELDS], order_id),
        )
        conn.commit()


# ---- order lines ----

def _loaded_price_at(conn, part_id: int | None, qty: float,
                     overhead_default: float) -> float | None:
    """The price for a part at the ordered quantity = its LOADED cost (volume-aware BOM rollup,
    material × overhead) — what the customer is charged for the parts. None when the part carries no
    price/cost info. (Production cost + profit are a separate 97- part priced outside PartPilot.)"""
    if not part_id:
        return None
    return pricing.rolled_sell_price(conn, part_id, qty or 1, overhead_default)


def add_line(db: Database, order_id: int, part_id: int | None, qty: float,
             unit_price: float | None, discount: float | None) -> None:
    overhead = setup_repo.get_default_markup(db)
    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM customer_orders WHERE id = ?", (order_id,)).fetchone() is None:
            raise ValueError("Order not found.")
        overridden = 0
        if part_id is not None:
            if conn.execute("SELECT 1 FROM parts WHERE id = ?", (part_id,)).fetchone() is None:
                raise ValueError("Selected product was not found.")
            if unit_price is None:   # default to the loaded parts price at the ordered volume
                unit_price = _loaded_price_at(conn, part_id, qty or 1, overhead)
            else:
                overridden = 1       # operator typed a price -> don't auto-reprice it later
        elif unit_price is not None:
            overridden = 1
        next_no = conn.execute(
            "SELECT COALESCE(MAX(line_no), 0) + 1 FROM customer_order_lines WHERE order_id = ?",
            (order_id,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO customer_order_lines "
            "(order_id, part_id, line_no, ordered_qty, unit_price, discount_percent, price_overridden) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (order_id, part_id, next_no, qty or 1, unit_price, discount, overridden),
        )
        conn.commit()


def update_line(db: Database, order_id: int, line_id: int, qty: float,
                unit_price: float | None, discount: float | None) -> None:
    """Save a line edit. A submitted ``unit_price`` is treated as an explicit override (and pins the
    price against future re-pricing); a blank price means "auto" and re-prices from the tiers at the
    new quantity."""
    overhead = setup_repo.get_default_markup(db)
    with db.connect() as conn:
        row = conn.execute(
            "SELECT part_id FROM customer_order_lines WHERE id = ? AND order_id = ?",
            (line_id, order_id),
        ).fetchone()
        if row is None:
            return
        if unit_price is not None:
            price, overridden = unit_price, 1
        else:                              # blank -> auto (re-price at the new qty)
            price = _loaded_price_at(conn, row["part_id"], qty or 1, overhead)
            overridden = 0
        conn.execute(
            "UPDATE customer_order_lines SET ordered_qty = ?, unit_price = ?, discount_percent = ?, "
            "price_overridden = ? WHERE id = ? AND order_id = ?",
            (qty or 0, price, discount, overridden, line_id, order_id),
        )
        conn.commit()


def reprice_line(db: Database, order_id: int, line_id: int) -> None:
    """Recompute a line's unit price from the current tiers at its ordered quantity and clear the
    manual-override flag."""
    overhead = setup_repo.get_default_markup(db)
    with db.connect() as conn:
        row = conn.execute(
            "SELECT part_id, ordered_qty FROM customer_order_lines WHERE id = ? AND order_id = ?",
            (line_id, order_id),
        ).fetchone()
        if row is None or not row["part_id"]:
            return
        price = _loaded_price_at(conn, row["part_id"], row["ordered_qty"] or 1, overhead)
        conn.execute(
            "UPDATE customer_order_lines SET unit_price = ?, price_overridden = 0 "
            "WHERE id = ? AND order_id = ?",
            (price, line_id, order_id),
        )
        conn.commit()


def delete_line(db: Database, order_id: int, line_id: int) -> None:
    with db.connect() as conn:
        conn.execute(
            "DELETE FROM customer_order_lines WHERE id = ? AND order_id = ?", (line_id, order_id)
        )
        conn.commit()


# ---- stock allocation ----

def _recompute_part_alloc(conn, part_id: int) -> None:
    """Roll a part's allocations up onto parts.total_alloc (and mirror onto its primary stock row)."""
    total = conn.execute(
        "SELECT COALESCE(SUM(qty), 0) FROM allocations WHERE part_id = ?", (part_id,)
    ).fetchone()[0]
    conn.execute("UPDATE parts SET total_alloc = ? WHERE id = ?", (total, part_id))
    row = conn.execute(
        "SELECT id FROM part_stock WHERE part_id = ? ORDER BY id LIMIT 1", (part_id,)
    ).fetchone()
    if row:
        conn.execute("UPDATE part_stock SET allocated = ? WHERE id = ?", (total, row["id"]))


def allocate_order(db: Database, order_id: int) -> float:
    """Reserve available stock to this order's lines (up to the outstanding qty per line, limited by
    each part's free stock). Returns the total newly allocated. Operator-triggered, re-runnable."""
    newly = 0.0
    with db.connect() as conn:
        lines = conn.execute(
            "SELECT id, part_id, ordered_qty FROM customer_order_lines WHERE order_id = ?", (order_id,)
        ).fetchall()
        for ln in lines:
            if not ln["part_id"]:
                continue
            already = conn.execute(
                "SELECT COALESCE(SUM(qty), 0) FROM allocations WHERE customer_order_line_id = ?",
                (ln["id"],),
            ).fetchone()[0]
            need = (ln["ordered_qty"] or 0) - already
            if need <= 0:
                continue
            part = conn.execute("SELECT total_qty, total_alloc FROM parts WHERE id = ?",
                                (ln["part_id"],)).fetchone()
            free = (part["total_qty"] or 0) - (part["total_alloc"] or 0)
            take = min(need, free)
            if take > 0:
                conn.execute(
                    "INSERT INTO allocations (customer_order_line_id, part_id, qty) VALUES (?, ?, ?)",
                    (ln["id"], ln["part_id"], take),
                )
                _recompute_part_alloc(conn, ln["part_id"])  # lowers free for later lines of same part
                newly += take
        conn.commit()
    return newly


def release_order_allocations(db: Database, order_id: int) -> None:
    """Free all stock reserved to an order (e.g. on cancel or to re-allocate)."""
    with db.connect() as conn:
        _release_allocations(conn, order_id)
        conn.commit()


def _release_allocations(conn, order_id: int) -> None:
    part_ids = [r[0] for r in conn.execute(
        "SELECT DISTINCT part_id FROM allocations WHERE customer_order_line_id IN "
        "(SELECT id FROM customer_order_lines WHERE order_id = ?)", (order_id,))]
    conn.execute(
        "DELETE FROM allocations WHERE customer_order_line_id IN "
        "(SELECT id FROM customer_order_lines WHERE order_id = ?)", (order_id,))
    for pid in part_ids:
        _recompute_part_alloc(conn, pid)


def order_downstream(db: Database, order_id: int) -> dict:
    """What this customer order spawned and which we don't auto-cancel: the work orders raised to
    fulfil it, and open purchase orders that include parts those work orders need. Surfaced when
    cancelling so the operator can review them."""
    with db.connect() as conn:
        wos = [dict(r) for r in conn.execute(
            """SELECT w.id, w.wo_no, w.status, p.part_no AS assembly_part_no
               FROM work_orders w JOIN parts p ON p.id = w.assembly_id
               WHERE w.customer_order_line_id IN
                     (SELECT id FROM customer_order_lines WHERE order_id = ?)
               ORDER BY w.id""", (order_id,))]
        pos = []
        if wos:
            qmarks = ",".join("?" * len(wos))
            pos = [dict(r) for r in conn.execute(
                f"""SELECT DISTINCT po.id, po.po_no, po.status, s.name AS supplier_name
                    FROM purchase_orders po
                    JOIN purchase_order_lines pl ON pl.po_id = po.id
                    LEFT JOIN suppliers s ON s.id = po.supplier_id
                    WHERE po.status IN ('draft', 'ordered')
                      AND pl.part_id IN (SELECT part_id FROM work_order_lines
                                         WHERE work_order_id IN ({qmarks}))
                    ORDER BY po.id""", [w["id"] for w in wos])]
    return {"work_orders": wos, "purchase_orders": pos}


# ---- order acknowledgement (customer-facing PDF, retained for ISO) ----

def acknowledge_order(db: Database, order_id: int, user: str | None = None) -> int:
    """Generate the order-acknowledgement PDF, store it immutably in ``co_documents`` and advance a
    still-draft order to 'confirmed'. Re-issuable after amendments (a new version is appended; older
    versions are kept). Returns the new document id."""
    order = get_order(db, order_id)
    if order is None:
        raise ValueError("Order not found.")
    if order["status"] not in ACK_STATUSES:
        raise ValueError(f"A {order['status']} order can't be acknowledged.")
    if not order["lines"]:
        raise ValueError("Add at least one line before acknowledging the order.")
    ref = order["order_ref"] or ref_no("CO", order_id)
    content = export.ack_pdf(order, export._company(db))
    filename = f"OA-{ref}.pdf"
    with db.connect() as conn:
        doc_id = conn.execute(
            "INSERT INTO co_documents (order_id, kind, filename, content, byte_size, created_by) "
            "VALUES (?, 'pdf', ?, ?, ?, ?)",
            (order_id, filename, content, len(content), user),
        ).lastrowid
        if order["status"] == "draft" and _ack_confirms(conn):
            conn.execute("UPDATE customer_orders SET status = 'confirmed', updated_at = datetime('now') "
                         "WHERE id = ?", (order_id,))
        conn.commit()
    return doc_id


def _ack_confirms(conn) -> bool:
    """Whether acknowledging a draft order also confirms it (Setup -> Order settings). Defaults to
    True (unchanged behaviour); only an explicit '0' turns it off. Read directly and defensively so
    this feature stays decoupled from the setup feature and works even if the table is absent."""
    try:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = 'orders.ack_confirms'").fetchone()
    except Exception:
        return True
    return row is None or row["value"] != "0"


def get_document(db: Database, order_id: int, kind: str = "pdf") -> dict | None:
    """The latest archived acknowledgement of ``kind`` for an order."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT filename, content FROM co_documents WHERE order_id = ? AND kind = ? "
            "ORDER BY id DESC LIMIT 1", (order_id, kind)).fetchone()
    return dict(row) if row else None


def documents_for_order(db: Database, order_id: int) -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, kind, filename, byte_size, created_by, created_at FROM co_documents "
            "WHERE order_id = ? ORDER BY id DESC", (order_id,))]


def cancel_order(db: Database, order_id: int) -> None:
    """Cancel a customer order and roll back any reserved stock (allocations). Only before the
    order has shipped — once goods are out it's a returns/credit process, not a cancel."""
    with db.connect() as conn:
        row = conn.execute("SELECT status FROM customer_orders WHERE id = ?", (order_id,)).fetchone()
        if row is None:
            raise ValueError("Order not found.")
        if row["status"] in ("shipped", "complete", "cancelled"):
            raise ValueError(f"A {row['status']} order can't be cancelled.")
        _release_allocations(conn, order_id)        # free the reserved stock
        conn.execute("UPDATE customer_orders SET status = 'cancelled', updated_at = datetime('now') "
                     "WHERE id = ?", (order_id,))
        conn.commit()
