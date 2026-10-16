"""Build a ``StockIndex`` from PartPilot's own catalog.

This is the "check our own stock first" source for BOM resolution: before querying Digi-Key/Mouser,
the resolver asks this index whether a line is already covered by free stock. It replaces the old
miniMRP stock read — PartPilot's catalog now holds the same fields (canonical P/N, manufacturer P/N,
the ``value`` notation, category, on-hand/allocated), so the generic matching in
``partpilot.stock`` works unchanged.
"""

from __future__ import annotations

from ....stock import StockIndex, StockItem, comp_type_for_category, parse_value
from ...core.db import Database


def build_stock_index(db: Database, location_ids: list[int] | None = None) -> StockIndex:
    """Index every stockable catalog part (documents excluded) for BOM matching by MPN / value, with
    on-hand/allocated summed over ``location_ids`` only — a customer's shelf sees only their stock.
    ``None`` means all of OUR shelves (never a customer's); an empty list indexes nothing."""
    if location_ids is None:
        with db.connect() as conn:
            location_ids = [r["id"] for r in conn.execute(
                "SELECT id FROM stock_locations WHERE kind = 'ours' AND retired_at IS NULL")]
    if not location_ids:
        return StockIndex.build([])
    marks = ",".join("?" for _ in location_ids)
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT p.id, p.part_no, COALESCE(p.value, '') AS value, "
            f"       COALESCE(p.description, '') AS description, COALESCE(p.category, '') AS category, "
            f"       COALESCE(p.mfr_pno, '') AS mfr_pno, "
            f"       COALESCE((SELECT SUM(pk.on_hand) FROM part_stock pk "
            f"                 WHERE pk.part_id = p.id AND pk.location_id IN ({marks})), 0) AS total_qty, "
            f"       COALESCE((SELECT SUM(pk.allocated) FROM part_stock pk "
            f"                 WHERE pk.part_id = p.id AND pk.location_id IN ({marks})), 0) AS total_alloc "
            f"FROM parts p WHERE COALESCE(p.is_document, 0) = 0 "
            # Cover BOM lines only from the current (released) version — a draft/superseded version
            # carries its own separate (often zero/stale) stock that shouldn't satisfy a build.
            f"AND p.version_status = 'released' "
            # A retired part's stock must never satisfy a line: retired means run out, don't re-buy,
            # and the resolver must not steer a new build onto it.
            f"AND p.retired_at IS NULL",
            [*location_ids, *location_ids],
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
