"""Bulk load logic for the catalog, over already-parsed row dicts.

``import_tables`` upserts suppliers / parts / part-suppliers / stock keyed by ``minimrp_id`` (the
legacy source id, retained purely as an idempotency key). It reads nothing external — callers pass
row dicts — so it stays a plain, testable bulk-insert helper. (The miniMRP mdb reader that used to
feed it was removed when miniMRP was decommissioned.)
"""

from __future__ import annotations

from ...core.db import Database


def _f(x: str | None, default: float | None = 0.0) -> float | None:
    try:
        return float(x) if x not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _i(x: str | None) -> int | None:
    try:
        return int(float(x)) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _s(x: str | None) -> str | None:
    s = (x or "").strip()
    return s or None


def import_tables(
    db: Database,
    *,
    suppliers: list[dict],
    parts: list[dict],
    item_suppliers: list[dict],
    item_locations: list[dict],
) -> dict[str, int]:
    """Upsert miniMRP rows into the catalog tables. Returns per-table counts."""
    with db.connect() as conn:
        # --- suppliers ---
        for r in suppliers:
            conn.execute(
                """INSERT INTO suppliers (name, short_name, url, currency, minimrp_id)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(minimrp_id) DO UPDATE SET
                     name=excluded.name, short_name=excluded.short_name,
                     url=excluded.url, currency=excluded.currency""",
                (_s(r.get("CoName")) or "?", _s(r.get("ShortNm")), _s(r.get("URL")),
                 _s(r.get("defCurrency")), _i(r.get("AddID"))),
            )
        sup_map = {row["minimrp_id"]: row["id"] for row in
                   conn.execute("SELECT id, minimrp_id FROM suppliers WHERE minimrp_id IS NOT NULL")}

        # --- parts ---
        from .repo import is_document_part_no  # late import: repo pulls in pricing etc.

        for r in parts:
            part_no = _s(r.get("MasterPNo")) or "?"
            # Document-class codes (5x/95) are always documents + excluded from BOM cost — same rule
            # create_part/update_part enforce. max() on conflict so a manually-set flag never drops.
            doc = 1 if is_document_part_no(part_no) else 0
            conn.execute(
                """INSERT INTO parts
                   (part_no, value, description, category, kind, mfr_name, mfr_pno, rev,
                    unit_cost, min_qty, total_qty, total_alloc, total_on_order, minimrp_id,
                    is_document, exclude_from_bom_cost)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(minimrp_id) DO UPDATE SET
                     part_no=excluded.part_no, value=excluded.value, description=excluded.description,
                     category=excluded.category, kind=excluded.kind, mfr_name=excluded.mfr_name,
                     mfr_pno=excluded.mfr_pno, rev=excluded.rev, unit_cost=excluded.unit_cost,
                     min_qty=excluded.min_qty, total_qty=excluded.total_qty,
                     total_alloc=excluded.total_alloc, total_on_order=excluded.total_on_order,
                     is_document=max(parts.is_document, excluded.is_document),
                     exclude_from_bom_cost=max(parts.exclude_from_bom_cost, excluded.exclude_from_bom_cost),
                     updated_at=datetime('now')""",
                (part_no, _s(r.get("ItemName")), _s(r.get("ItemDescription")),
                 (_s(r.get("Category")) or "").upper() or None,
                 (_s(r.get("Type")) or "PART").upper(), _s(r.get("MfrName")), _s(r.get("MfrPNo")),
                 _s(r.get("Rev")), _f(r.get("xCost"), None), _f(r.get("MinQty")), _f(r.get("TotalQty")),
                 _f(r.get("TotalAllocQty")), _f(r.get("TotalOnOrderQty")), _i(r.get("ItemID")),
                 doc, doc),
            )
        part_map = {row["minimrp_id"]: row["id"] for row in
                    conn.execute("SELECT id, minimrp_id FROM parts WHERE minimrp_id IS NOT NULL")}

        # --- locations (derived; miniMRP has no location master table) ---
        loc_ids = sorted({lid for r in item_locations if (lid := _i(r.get("LocLocationID"))) is not None})
        for lid in loc_ids:
            name = "Main" if lid == 1 else f"Location {lid}"
            conn.execute(
                "INSERT INTO stock_locations (name, minimrp_id) VALUES (?, ?) "
                "ON CONFLICT(minimrp_id) DO UPDATE SET name=excluded.name",
                (name, lid),
            )
        loc_map = {row["minimrp_id"]: row["id"] for row in
                   conn.execute("SELECT id, minimrp_id FROM stock_locations WHERE minimrp_id IS NOT NULL")}

        # --- part_suppliers ---
        ps = 0
        for r in item_suppliers:
            pid = part_map.get(_i(r.get("Supplier_ItemID")))
            if pid is None:
                continue
            conn.execute(
                """INSERT INTO part_suppliers
                   (part_id, supplier_id, supplier_pno, price_per_uom, qty_per_uom, moq,
                    lead_time, is_default, minimrp_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(minimrp_id) DO UPDATE SET
                     part_id=excluded.part_id, supplier_id=excluded.supplier_id,
                     supplier_pno=excluded.supplier_pno, price_per_uom=excluded.price_per_uom,
                     qty_per_uom=excluded.qty_per_uom, moq=excluded.moq, lead_time=excluded.lead_time,
                     is_default=excluded.is_default""",
                (pid, sup_map.get(_i(r.get("SupplierID"))), _s(r.get("SupplierPNo")),
                 _f(r.get("PriceEach"), None), _f(r.get("QtyPerUOM")) or 1, _f(r.get("MinOrQty"), None),
                 _i(r.get("LeadTime")), 1 if r.get("DefaultSupplier") == "1" else 0, _i(r.get("AutoID"))),
            )
            ps += 1

        # --- part_stock ---
        st = 0
        for r in item_locations:
            pid = part_map.get(_i(r.get("LocStockID")))
            if pid is None:
                continue
            conn.execute(
                """INSERT INTO part_stock
                   (part_id, location_id, bin, on_hand, allocated, on_order, minimrp_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(minimrp_id) DO UPDATE SET
                     part_id=excluded.part_id, location_id=excluded.location_id, bin=excluded.bin,
                     on_hand=excluded.on_hand, allocated=excluded.allocated, on_order=excluded.on_order""",
                (pid, loc_map.get(_i(r.get("LocLocationID"))), _s(r.get("LocBIN")),
                 _f(r.get("LocOnHandQty")), _f(r.get("LocAllocQty")), _f(r.get("LocOnOrderQty")),
                 _i(r.get("AutoID"))),
            )
            st += 1

        conn.commit()
    return {"suppliers": len(sup_map), "parts": len(part_map), "locations": len(loc_map),
            "part_suppliers": ps, "part_stock": st}
