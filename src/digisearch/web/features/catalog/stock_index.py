"""Build a ``StockIndex`` from PartPilot's own catalog.

This is the "check our own stock first" source for BOM resolution: before querying Digi-Key/Mouser,
the resolver asks this index whether a line is already covered by free stock. It replaces the old
miniMRP stock read — PartPilot's catalog now holds the same fields (canonical P/N, manufacturer P/N,
the ``value`` notation, category, on-hand/allocated), so the generic matching in
``digisearch.stock`` works unchanged.
"""

from __future__ import annotations

from ....stock import StockIndex, StockItem, comp_type_for_category, parse_value
from ...core.db import Database


def build_stock_index(db: Database) -> StockIndex:
    """Index every stockable catalog part (documents excluded) for BOM matching by MPN / value."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, part_no, COALESCE(value, '') AS value, "
            "       COALESCE(description, '') AS description, COALESCE(category, '') AS category, "
            "       COALESCE(mfr_pno, '') AS mfr_pno, "
            "       COALESCE(total_qty, 0) AS total_qty, COALESCE(total_alloc, 0) AS total_alloc "
            "FROM parts WHERE COALESCE(is_document, 0) = 0"
        ).fetchall()
    items: list[StockItem] = []
    for r in rows:
        comp_type = comp_type_for_category(r["category"])
        value_si, package = parse_value(r["value"], comp_type, r["description"])
        items.append(StockItem(
            item_id=r["id"],
            master_pno=(r["part_no"] or "").strip(),   # PartPilot's canonical P/N (often the MPN)
            mfr_pno=(r["mfr_pno"] or "").strip(),
            name=(r["value"] or "").strip(),
            description=(r["description"] or "").strip(),
            category=(r["category"] or "").strip(),
            comp_type=comp_type,
            value_si=value_si,
            package=package,
            on_hand=r["total_qty"] or 0.0,
            allocated=r["total_alloc"] or 0.0,
            on_order=0.0,
        ))
    return StockIndex.build(items)
