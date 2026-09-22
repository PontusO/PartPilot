"""Purchase order queries, the shortage→suggested-PO analyser, and goods receiving.

Receiving posts RECEIVE movements through catalog.stock.post_movement, so received goods land in
inventory and the ledger together. "On order" is derived live from open (ordered) PO lines rather
than denormalised onto parts.
"""

from __future__ import annotations

from ...core import ref_no
from ...core.db import Database
from ..catalog import cost_refresh, pricing, stock
from ..catalog import repo as catrepo
from ..setup import repo as setup_repo
from . import export

STATUSES = ("draft", "ordered", "received", "cancelled")


def _unit_price(price_per_uom, qty_per_uom):
    if price_per_uom is None:
        return None
    return price_per_uom / qty_per_uom if qty_per_uom else price_per_uom


def _default_supplier(conn, part_id):
    """The part's preferred supplier offer (default flag, else lowest id)."""
    return conn.execute(
        """SELECT ps.id AS part_supplier_id, ps.supplier_pno, ps.price_per_uom, ps.qty_per_uom,
                  ps.moq, s.id AS supplier_id, s.name AS supplier_name
           FROM part_suppliers ps LEFT JOIN suppliers s ON s.id = ps.supplier_id
           WHERE ps.part_id = ? ORDER BY ps.is_default DESC, ps.id LIMIT 1""",
        (part_id,),
    ).fetchone()


def _stored_tier_price(conn, part_supplier_id, qty):
    """The cut-tape cost tier for ``qty`` from the offer's *stored* tiers, or None if it has none."""
    if not part_supplier_id:
        return None
    tiers = pricing.load_cost_tiers(conn, part_supplier_id, "cut")
    return pricing.price_at(tiers, qty) if tiers else None


def _priced_line(db: Database, clients, sup, qty) -> float | None:
    """Unit price for buying ``qty`` from offer ``sup`` (a ``_default_supplier`` row). If the offer is
    a configured distributor, re-query it: overwrite its cost tiers from the live price breaks, set the
    offer's flat unit price to the tier at ``qty``, and return that. Otherwise (or on any lookup
    failure) price from the offer's stored cost tiers at ``qty``, falling back to its flat unit price.
    Network + tier writes here run OUTSIDE any PO-insert transaction."""
    ps_id = sup["part_supplier_id"]
    breaks = cost_refresh.fetch_offer_breaks(clients, sup["supplier_name"], sup["supplier_pno"])
    # Only trust a live response that carries a cut-tape ladder — pricing and the sell-tier rollup are
    # cut-based. A reel-only reply must NOT wipe the offer's existing cut cost tiers, so fall through
    # to stored/flat pricing in that case.
    if breaks is not None and ps_id and breaks[0]:
        cut, reel = breaks
        catrepo.replace_cost_tiers(db, ps_id, cut, reel)
        price = pricing.price_at([(t["break_qty"], t["unit_price"]) for t in cut], qty)
        if price is not None:
            catrepo.set_offer_unit_price_db(db, ps_id, price)   # update the offer's current price
            return price
    with db.connect() as conn:
        stored = _stored_tier_price(conn, ps_id, qty)
    if stored is not None:
        return stored
    return _unit_price(sup["price_per_uom"], sup["qty_per_uom"])


def _offer_for_receipt(conn, part_id, supplier_id, supplier_pno):
    """The ``part_suppliers`` offer a receipt should update: this supplier's offer, preferring an
    exact supplier-P/N match, then the default/lowest-id offer. None if the part has no offer from
    this supplier (e.g. it was bought elsewhere) — in which case the receipt writes no price back."""
    if not supplier_id:
        return None
    return conn.execute(
        """SELECT id, qty_per_uom FROM part_suppliers
           WHERE part_id = ? AND supplier_id = ?
           ORDER BY (CASE WHEN supplier_pno IS ? THEN 0 ELSE 1 END), is_default DESC, id LIMIT 1""",
        (part_id, supplier_id, supplier_pno),
    ).fetchone()


# ---- reads ----

def summary(db: Database) -> dict:
    with db.connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0]
        rows = dict(conn.execute(
            "SELECT status, COUNT(*) FROM purchase_orders GROUP BY status").fetchall())
        outstanding = conn.execute(
            "SELECT COALESCE(SUM((pl.qty - pl.qty_received) * COALESCE(pl.unit_price, 0)), 0) "
            "FROM purchase_order_lines pl JOIN purchase_orders p ON p.id = pl.po_id "
            "WHERE p.status = 'ordered'").fetchone()[0]
    return {"total": total, "draft": rows.get("draft", 0), "ordered": rows.get("ordered", 0),
            "on_order_value": outstanding}


def suppliers(db: Database) -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT id, name FROM suppliers ORDER BY name")]


def parts_for_picker(db: Database) -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, part_no, value, kind, total_qty, unit_cost FROM parts ORDER BY part_no")]


def list_pos(db: Database, status: str | None = None, search: str | None = None) -> list[dict]:
    like = f"%{search}%" if search else None
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT p.id, p.po_no, p.status, p.order_date, p.required_date, p.currency,
                      s.name AS supplier_name,
                      (SELECT COUNT(*) FROM purchase_order_lines l WHERE l.po_id = p.id) AS line_count,
                      (SELECT COALESCE(SUM(l.qty * COALESCE(l.unit_price, 0)), 0)
                       FROM purchase_order_lines l WHERE l.po_id = p.id) AS total
               FROM purchase_orders p LEFT JOIN suppliers s ON s.id = p.supplier_id
               WHERE (:status IS NULL OR p.status = :status)
                 AND (:search IS NULL OR p.po_no LIKE :like OR s.name LIKE :like)
               ORDER BY p.id DESC""",
            {"status": status, "search": search, "like": like},
        )]


def get_po(db: Database, po_id: int) -> dict | None:
    with db.connect() as conn:
        head = conn.execute(
            """SELECT p.*, s.name AS supplier_name FROM purchase_orders p
               LEFT JOIN suppliers s ON s.id = p.supplier_id WHERE p.id = ?""",
            (po_id,),
        ).fetchone()
        if head is None:
            return None
        lines = [dict(r) for r in conn.execute(
            """SELECT l.id, l.part_id, l.supplier_pno, l.qty, l.unit_price, l.qty_received, l.line_no,
                      pt.part_no, pt.value, pt.kind, pt.mfr_pno, pt.description
               FROM purchase_order_lines l LEFT JOIN parts pt ON pt.id = l.part_id
               WHERE l.po_id = ? ORDER BY COALESCE(l.line_no, 1e9), l.id""",
            (po_id,),
        )]
    po = dict(head)
    total = 0.0
    for ln in lines:
        ln["line_total"] = (ln["qty"] or 0) * (ln["unit_price"] or 0)
        ln["outstanding"] = max(0.0, (ln["qty"] or 0) - (ln["qty_received"] or 0))
        total += ln["line_total"]
    po["lines"] = lines
    po["total"] = total
    po["outstanding_qty"] = sum(ln["outstanding"] for ln in lines)
    return po


# ---- shortage analyser (automation: what to buy so planned builds can complete) ----

def shortage_suggestions(db: Database) -> list[dict]:
    """Components demanded by allocated work orders but not covered by free stock + open POs.

    Returns one row per short component, with its preferred supplier and a suggested order qty.
    Operator reviews and confirms before any PO is created.
    """
    with db.connect() as conn:
        demand = dict(conn.execute(
            "SELECT wl.part_id, SUM(wl.qty_required) FROM work_order_lines wl "
            "JOIN work_orders w ON w.id = wl.work_order_id WHERE w.status = 'allocated' "
            "GROUP BY wl.part_id").fetchall())
        if not demand:
            return []
        on_order = dict(conn.execute(
            "SELECT part_id, SUM(qty - qty_received) FROM purchase_order_lines pl "
            "JOIN purchase_orders p ON p.id = pl.po_id WHERE p.status = 'ordered' "
            "GROUP BY part_id").fetchall())
        out = []
        for part_id, required in demand.items():
            part = conn.execute(
                "SELECT part_no, value, total_qty, total_alloc, unlimited_stock "
                "FROM parts WHERE id = ?", (part_id,)
            ).fetchone()
            if part is None or part["unlimited_stock"]:
                continue  # unlimited parts (e.g. SMT Assembly) never run short -> never purchased
            free = (part["total_qty"] or 0) - (part["total_alloc"] or 0)
            incoming = on_order.get(part_id, 0) or 0
            short = required - free - incoming
            if short <= 0:
                continue
            sup = _default_supplier(conn, part_id)
            # Preview the price at the suggested qty using the offer's stored cost tiers (no network);
            # PO generation re-prices live. Falls back to the flat unit price.
            if sup:
                price = _stored_tier_price(conn, sup["part_supplier_id"], short)
                if price is None:
                    price = _unit_price(sup["price_per_uom"], sup["qty_per_uom"])
            else:
                price = None
            out.append({
                "part_id": part_id, "part_no": part["part_no"], "value": part["value"],
                "required": required, "free": free, "on_order": incoming, "short": short,
                "suggested_qty": short,
                "supplier_id": sup["supplier_id"] if sup else None,
                "supplier_name": sup["supplier_name"] if sup else None,
                "supplier_pno": sup["supplier_pno"] if sup else None,
                "unit_price": price,
            })
    out.sort(key=lambda r: (r["supplier_name"] or "~", r["part_no"]))
    return out


def shortage_suggestions_grouped(db: Database) -> list[dict]:
    """Shortage rows grouped by supplier: [{supplier_id, supplier_name, lines:[...]}, ...].
    The no-supplier group (supplier_id None) sorts last (its parts can't be auto-ordered)."""
    groups: dict = {}
    for s in shortage_suggestions(db):
        g = groups.setdefault(s["supplier_id"],
                              {"supplier_id": s["supplier_id"], "supplier_name": s["supplier_name"],
                               "lines": []})
        g["lines"].append(s)
    return list(groups.values())


def create_pos_from_suggestions(db: Database, selections: dict[int, float], user=None) -> list[int]:
    """Create one draft PO per supplier from selected {part_id: qty}. Parts with no supplier
    are skipped (can't be auto-ordered). Returns the new PO ids.

    Each line is priced tier-aware at its buy quantity — for distributor offers this re-queries the
    supplier and refreshes the cost tiers (see :func:`_priced_line`). That network + tier-write step
    runs first (outside the PO-insert transaction) so no HTTP happens while holding a write lock."""
    # Phase 1: resolve offers and price each line (may hit the network + write cost tiers).
    clients = cost_refresh.build_clients()
    markup = setup_repo.get_default_markup(db)
    by_supplier: dict[int, list] = {}
    for part_id, qty in selections.items():
        if not qty or qty <= 0:
            continue
        with db.connect() as conn:
            sup = _default_supplier(conn, part_id)
        if sup is None or sup["supplier_id"] is None:
            continue
        price = _priced_line(db, clients, sup, qty)
        by_supplier.setdefault(sup["supplier_id"], []).append(
            (part_id, qty, sup["supplier_pno"], price))

    # Phase 2: insert the POs/lines in one transaction (no network here).
    created = []
    with db.connect() as conn:
        for supplier_id, lines in by_supplier.items():
            po_id = conn.execute(
                "INSERT INTO purchase_orders (supplier_id, status, order_date) VALUES (?, 'draft', date('now'))",
                (supplier_id,),
            ).lastrowid
            conn.execute("UPDATE purchase_orders SET po_no = ? WHERE id = ?", (ref_no("PO", po_id), po_id))
            for i, (part_id, qty, pno, price) in enumerate(lines, start=1):
                conn.execute(
                    "INSERT INTO purchase_order_lines (po_id, part_id, supplier_pno, qty, unit_price, line_no) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (po_id, part_id, pno, qty, price, i),
                )
            created.append(po_id)
        conn.commit()

    # Phase 3: re-anchor sell tiers to the ordered price — only now that the POs actually exist.
    for lines in by_supplier.values():
        for part_id, qty, _pno, price in lines:
            catrepo.recalc_sell_tiers_from_purchase(db, part_id, qty, price, markup)
    return created


# ---- manual create + lines ----

def create_po(db: Database, data: dict) -> int:
    with db.connect() as conn:
        po_id = conn.execute(
            "INSERT INTO purchase_orders (po_no, supplier_id, status, order_date, required_date, "
            "currency, notes) VALUES (?, ?, 'draft', ?, ?, ?, ?)",
            (data.get("po_no"), data.get("supplier_id"), data.get("order_date"),
             data.get("required_date"), data.get("currency"), data.get("notes")),
        ).lastrowid
        if not data.get("po_no"):
            conn.execute("UPDATE purchase_orders SET po_no = ? WHERE id = ?", (ref_no("PO", po_id), po_id))
        conn.commit()
    return po_id


def add_line(db: Database, po_id: int, part_id: int | None, qty: float,
             unit_price: float | None) -> None:
    # Validate + resolve the offer in a read pass, then price (a distributor offer with no explicit
    # price re-queries the supplier), then insert — so no HTTP runs inside the write transaction.
    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM purchase_orders WHERE id = ?", (po_id,)).fetchone() is None:
            raise ValueError("Purchase order not found.")
        sup = None
        if part_id is not None:
            if conn.execute("SELECT 1 FROM parts WHERE id = ?", (part_id,)).fetchone() is None:
                raise ValueError("Selected part was not found.")
            sup = _default_supplier(conn, part_id)
    supplier_pno = sup["supplier_pno"] if sup else None
    if unit_price is None and sup is not None:
        unit_price = _priced_line(db, cost_refresh.build_clients(), sup, qty or 1)
    with db.connect() as conn:
        next_no = conn.execute(
            "SELECT COALESCE(MAX(line_no), 0) + 1 FROM purchase_order_lines WHERE po_id = ?", (po_id,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO purchase_order_lines (po_id, part_id, supplier_pno, qty, unit_price, line_no) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (po_id, part_id, supplier_pno, qty or 1, unit_price, next_no),
        )
        conn.commit()
    # Re-anchor the part's sell tiers to this line's price — only after the line is actually added.
    if part_id is not None and unit_price is not None:
        catrepo.recalc_sell_tiers_from_purchase(
            db, part_id, qty or 1, unit_price, setup_repo.get_default_markup(db))


def update_line(db: Database, po_id: int, line_id: int, qty: float,
                unit_price: float | None) -> None:
    """Change a line's order quantity and unit price. Only on a draft PO (before it's placed)."""
    with db.connect() as conn:
        po = conn.execute("SELECT status FROM purchase_orders WHERE id = ?", (po_id,)).fetchone()
        if po is None:
            raise ValueError("Purchase order not found.")
        if po["status"] != "draft":
            raise ValueError("Lines can only be changed on a draft PO (before it's placed).")
        conn.execute("UPDATE purchase_order_lines SET qty = ?, unit_price = ? WHERE id = ? AND po_id = ?",
                     (qty or 0, unit_price, line_id, po_id))
        row = conn.execute("SELECT part_id FROM purchase_order_lines WHERE id = ?", (line_id,)).fetchone()
        conn.commit()
    # Editing the ordered price/qty re-anchors the part's sell tiers.
    if row is not None and row["part_id"] is not None and unit_price is not None:
        catrepo.recalc_sell_tiers_from_purchase(
            db, row["part_id"], qty or 1, unit_price, setup_repo.get_default_markup(db))


def delete_line(db: Database, po_id: int, line_id: int) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM purchase_order_lines WHERE id = ? AND po_id = ?", (line_id, po_id))
        conn.commit()


# ---- lifecycle ----

def mark_ordered(db: Database, po_id: int, user: str | None = None) -> None:
    """Place a draft PO: freeze the supplier CSV + PDF as immutable ISO records and flip to
    'ordered' — atomically, so a placed PO always has its archived documents."""
    po = get_po(db, po_id)
    if po is None:
        raise ValueError("Purchase order not found.")
    if po["status"] != "draft":
        raise ValueError(f"Only a draft PO can be placed (this one is {po['status']}).")
    ref = po["po_no"] or ref_no("PO", po_id)
    docs = [
        ("csv", f"{ref}.csv", export.po_csv(po).encode("utf-8")),
        ("pdf", f"{ref}.pdf", export.po_pdf(po, export._company(db))),
    ]
    with db.connect() as conn:
        conn.execute("UPDATE purchase_orders SET status = 'ordered', updated_at = datetime('now') WHERE id = ?",
                     (po_id,))
        for kind, filename, content in docs:
            conn.execute(
                "INSERT INTO po_documents (po_id, kind, filename, content, byte_size, placed_by) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (po_id, kind, filename, content, len(content), user),
            )
        conn.commit()


def get_document(db: Database, po_id: int, kind: str) -> dict | None:
    """The latest archived document of ``kind`` ('csv'|'pdf') for a PO."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT filename, content FROM po_documents WHERE po_id = ? AND kind = ? "
            "ORDER BY id DESC LIMIT 1", (po_id, kind)).fetchone()
    return dict(row) if row else None


def documents_for_po(db: Database, po_id: int) -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, kind, filename, byte_size, placed_by, placed_at FROM po_documents "
            "WHERE po_id = ? ORDER BY id", (po_id,))]


def cancel_po(db: Database, po_id: int) -> None:
    with db.connect() as conn:
        po = conn.execute("SELECT status FROM purchase_orders WHERE id = ?", (po_id,)).fetchone()
        if po is None:
            raise ValueError("Purchase order not found.")
        if po["status"] not in ("draft", "ordered"):
            raise ValueError("Only a draft or ordered PO can be cancelled.")
        conn.execute("UPDATE purchase_orders SET status = 'cancelled', updated_at = datetime('now') WHERE id = ?",
                     (po_id,))
        conn.commit()


def receipts_for_po(db: Database, po_id: int) -> list[dict]:
    """Goods Received Notes raised against a PO."""
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, grn_no, grn_date, advice_no, received_by FROM goods_receipts "
            "WHERE po_id = ? ORDER BY id", (po_id,))]


def delete_po(db: Database, po_id: int) -> None:
    """Permanently delete a PO and everything generated for it — its lines and archived CSV/PDF
    documents (removed by FK cascade). Allowed only while NO goods have been received against it
    (supplier cancelled, lost in transit, requirement dropped). Once a Goods Received Note exists
    the received stock + receipt are real records, so the PO is cancelled (retained) instead."""
    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM purchase_orders WHERE id = ?", (po_id,)).fetchone() is None:
            raise ValueError("Purchase order not found.")
        if conn.execute("SELECT 1 FROM goods_receipts WHERE po_id = ? LIMIT 1", (po_id,)).fetchone():
            raise ValueError("Goods have been received against this PO — it can't be deleted. "
                             "Cancel it instead to keep the receipt record.")
        conn.execute("DELETE FROM purchase_orders WHERE id = ?", (po_id,))  # lines + documents cascade
        conn.commit()


def receive_po(db: Database, po_id: int, receipts: dict[int, float], user=None,
               advice_no: str | None = None) -> int | None:
    """Receive goods against a PO. ``receipts`` = {line_id: qty}. Records a Goods Received Note,
    posts a RECEIVE movement per line (stock in) and bumps qty_received; the PO becomes 'received'
    once nothing is outstanding. Returns the new GRN id (or None if nothing was received)."""
    with db.connect() as conn:
        po = conn.execute("SELECT * FROM purchase_orders WHERE id = ?", (po_id,)).fetchone()
        if po is None:
            raise ValueError("Purchase order not found.")
        if po["status"] != "ordered":
            raise ValueError(
                f"Can't receive against a {po['status']} PO — place the order first." if po["status"] == "draft"
                else f"Can't receive against a {po['status']} PO.")

        # Cap every receipt at the line's outstanding quantity — an over-typed qty (or a re-posted
        # form) must not push qty_received past what was ordered.
        for ln in conn.execute(
            "SELECT l.id, l.qty, l.qty_received, p.part_no "
            "FROM purchase_order_lines l LEFT JOIN parts p ON p.id = l.part_id WHERE l.po_id = ?",
            (po_id,),
        ):
            qty = receipts.get(ln["id"])
            if qty and qty > 0:
                outstanding = (ln["qty"] or 0) - (ln["qty_received"] or 0)
                if qty > outstanding + 1e-9:
                    raise ValueError(
                        f"{ln['part_no'] or 'Line'}: receiving {qty:g} exceeds the outstanding "
                        f"{max(outstanding, 0):g}.")

        grn_id = None
        if any(q and q > 0 for q in receipts.values()):
            grn_id = conn.execute(
                "INSERT INTO goods_receipts (po_id, supplier_id, grn_date, advice_no, received_by) "
                "VALUES (?, ?, date('now'), ?, ?)",
                (po_id, po["supplier_id"], advice_no, user),
            ).lastrowid
            conn.execute("UPDATE goods_receipts SET grn_no = ? WHERE id = ?", (ref_no("GRN", grn_id), grn_id))

        for ln in conn.execute("SELECT * FROM purchase_order_lines WHERE po_id = ?", (po_id,)):
            qty = receipts.get(ln["id"])
            if qty and qty > 0 and ln["part_id"]:
                stock.post_movement(conn, ln["part_id"], delta=qty, mtype=stock.RECEIVE,
                                    reference=po["po_no"] or f"PO-{po_id}", note="goods receiving",
                                    user=user)
                conn.execute("UPDATE purchase_order_lines SET qty_received = qty_received + ? WHERE id = ?",
                             (qty, ln["id"]))
                conn.execute(
                    "INSERT INTO goods_receipt_lines (grn_id, po_line_id, part_id, qty, unit_price) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (grn_id, ln["id"], ln["part_id"], qty, ln["unit_price"]),
                )
                # Record the price paid as the supplier offer's "last purchase price". Stored per-UOM
                # (price_per_uom = per-piece x qty_per_uom) to match the catalog convention, so the
                # displayed per-piece unit price becomes exactly what was paid. parts.unit_cost is
                # deliberately left untouched (downstream BOM/WO/CO costs stay put).
                if ln["unit_price"] is not None:
                    offer = _offer_for_receipt(conn, ln["part_id"], po["supplier_id"],
                                               ln["supplier_pno"])
                    if offer is not None:
                        catrepo.set_offer_unit_price(conn, offer["id"], ln["unit_price"])
        remaining = conn.execute(
            "SELECT COALESCE(SUM(qty - qty_received), 0) FROM purchase_order_lines WHERE po_id = ?",
            (po_id,),
        ).fetchone()[0]
        status = "received" if remaining <= 0 else "ordered"
        conn.execute("UPDATE purchase_orders SET status = ?, updated_at = datetime('now') WHERE id = ?",
                     (status, po_id))
        conn.commit()
    return grn_id
