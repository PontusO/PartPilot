"""Shared parts-import engine: classify resolved BOM lines against the catalog and create
the accepted ones.

Used by two flows that must stay in step: the assembly BOM importer (which then adds a BOM line
per row) and the Setup "Import parts from BOM" tool (catalog parts only). The intermediate "plan"
is a plain JSON-serializable list so it can be saved between the review POST and the apply POST.
"""

from __future__ import annotations

from partpilot.config import load_category_rules
from partpilot.models import ResolvedLine, Status
from partpilot.spec.ilabs_value import build_value_string

from ...core.db import Database
from ..purchasing.service import ResolvedRun
from . import repo as catalog_repo
from .import_category import guess_category

# Per-line status on the review screen.
IN_INVENTORY = "in_inventory"   # already a catalog part -> auto-linked
NEW = "new"                     # resolved on Digi-Key/Mouser, not in catalog -> offer to create
UNRESOLVED = "unresolved"       # searched, no match, but has a value/MPN -> offer a bare create
MANUAL = "manual"               # under-specified (no value/MPN) -> needs a human, can't import
SKIP = "skip"                   # DNP / non-orderable -> not added
RETIRED = "retired"             # only catalog match is a retired part -> never linked, never created


def _target_mpn(line: ResolvedLine) -> str | None:
    if line.chosen and line.chosen.mpn:
        return line.chosen.mpn
    return line.stock_match  # IN_STOCK lines carry the matched stock MPN


def _category(line: ResolvedLine, rules: dict) -> str | None:
    """Catalog category for a part we are about to create; see ``import_category``."""
    return guess_category(line, rules)


def _value_for(line: ResolvedLine) -> tuple[str | None, list[str]]:
    """The part's ``value``: the iLabs slash notation for a passive (built from the parsed spec
    plus the distributor's parametrics), else the raw BOM value for ICs/connectors/etc. The
    second element lists spec fields we couldn't fill, so the importer can flag it for review.
    """
    built = build_value_string(line.spec, line.chosen)
    if built is not None:
        return built.value or None, built.missing
    return line.line.value or None, []   # non-passive: keep the raw BOM value, nothing to flag


def build_import_plan(db: Database, run: ResolvedRun, stock_location_id: int | None = None) -> list[dict]:
    """Turn resolved lines into a review plan (one JSON-serializable dict per BOM line). Rows that
    link to an existing catalog part also carry ``shelf_free`` — how many are free on the shelf the
    stock check ran against — so "in the catalog but not on this shelf" is visible."""
    from . import stock as stock_core
    rules = load_category_rules()
    plan: list[dict] = []
    for ln in run.resolved:
        item = {
            "refdes": ln.line.refdes_str,
            "qty": ln.line.qty,
            "orig": (ln.line.value or ln.line.device or ""),
            "note": ln.flag_reason or ln.status.value,
            "part_id": None, "part_no": None, "supplier_label": None,
            "product_url": ln.chosen.product_url if ln.chosen else None,
        }
        if ln.status in (Status.DNP, Status.NON_ORDERABLE):
            item["status"] = SKIP
            plan.append(item)
            continue
        if ln.status == Status.MANUAL:
            item["status"] = MANUAL
            item["part_no"] = (ln.line.value or ln.line.device or "").strip() or None
            plan.append(item)
            continue

        mpn = _target_mpn(ln)
        existing_id = catalog_repo.find_part_id_by_mpn(db, mpn)
        if existing_id is None:
            # The only catalog match is a retired part: never link to it and never create a duplicate
            # number. The row gets its own status so the review screen offers no accept box.
            retired_id = catalog_repo.find_part_id_by_mpn(db, mpn, include_retired=True)
            if retired_id is not None:
                old = catalog_repo.get_part(db, retired_id)
                item.update(status=RETIRED, part_no=old["part_no"],
                            note=f"matches retired part {old['part_no']}")
                plan.append(item)
                continue
        value, missing = _value_for(ln)
        # The BOM's own maker wins over the distributor's: the designer named it on purpose.
        bom_mfr = (ln.line.manufacturer or "").strip() or None
        if existing_id is not None:
            item.update(status=IN_INVENTORY, part_id=existing_id, part_no=mpn, shelf_free=None)
            if stock_location_id:
                with db.connect() as conn:
                    item["shelf_free"] = stock_core.free_on_shelf(conn, existing_id, stock_location_id)
        elif ln.chosen is not None:
            c = ln.chosen
            item.update(
                status=NEW, part_no=c.mpn, value=value, value_missing=missing,
                category=_category(ln, rules), mfr_name=bom_mfr or c.manufacturer, mfr_pno=c.mpn,
                supplier_name=c.supplier, supplier_pno=c.dk_part_number, unit_cost=c.unit_price,
                reel_qty=c.reel_qty or 1,
                # Auto-captured supplier cost breaks (per-piece) -> cost tiers on the created part.
                cost_tiers=[{"break_qty": bq, "unit_price": p} for bq, p in c.price_breaks],
                reel_tiers=[{"break_qty": bq, "unit_price": p} for bq, p in c.reel_price_breaks],
                supplier_label=f"{c.supplier or ''} {c.dk_part_number or ''}".strip(),
            )
        else:
            item.update(
                status=UNRESOLVED, part_no=(ln.line.value or ln.line.device or "").strip() or None,
                value=value, value_missing=missing, category=_category(ln, rules), mfr_name=bom_mfr,
            )
        plan.append(item)
    return plan


def create_part_from_item(db: Database, item: dict) -> int:
    part = {
        "part_no": item.get("part_no") or "?",
        "value": item.get("value"),
        "category": (item.get("category") or "").upper() or None,
        "mfr_name": item.get("mfr_name"),
        "mfr_pno": item.get("mfr_pno"),
        "min_qty": 0,
    }
    supplier_lines = []
    if item.get("supplier_name"):
        supplier_lines = [{
            "supplier_name": item["supplier_name"], "supplier_pno": item.get("supplier_pno"),
            "unit_price": item.get("unit_cost"), "reel_qty": item.get("reel_qty") or 1,
            "is_default": True,
            "cost_tiers": item.get("cost_tiers") or [],
            "reel_tiers": item.get("reel_tiers") or [],
        }]
    return catalog_repo.create_part(db, part=part, supplier_lines=supplier_lines, opening=None)


def apply_parts_plan(db: Database, plan: list[dict], accepted: set[int]) -> dict:
    """Create the accepted NEW/UNRESOLVED rows and link IN_INVENTORY rows.

    Returns counts plus ``review`` (created passives with an incomplete iLabs value) and
    ``part_ids``: one entry per plan row, the catalog part id that row now points at, or None
    for rows that were skipped / not accepted / manual. ``created_rows`` lists the plan indexes
    whose part was created by this call (the rest of the non-None rows were linked). ``retired``
    lists the rows whose only catalog match is a retired part — never linked, never created — so
    the caller can show the operator which BOM lines were left out.
    """
    created = linked = skipped = 0
    review: list[dict] = []
    retired: list[dict] = []
    part_ids: list[int | None] = []
    created_rows: list[int] = []
    for i, item in enumerate(plan):
        status = item.get("status")
        if status == IN_INVENTORY:
            part_ids.append(item["part_id"])
            linked += 1
            continue
        if status == RETIRED:
            skipped += 1
            retired.append({"refdes": item.get("refdes"), "part_no": item.get("part_no")})
            part_ids.append(None)
            continue
        if status in (NEW, UNRESOLVED) and i in accepted:
            # Re-use an existing catalog part if its MPN already landed (avoids duplicates,
            # including the same MPN appearing twice in one file).
            pid = catalog_repo.find_part_id_by_mpn(db, item.get("part_no"))
            if pid is None and catalog_repo.find_part_id_by_mpn(
                    db, item.get("part_no"), include_retired=True) is not None:
                skipped += 1          # only match is retired: never link, never duplicate the number
                part_ids.append(None)
                continue
            if pid is None:
                pid = create_part_from_item(db, item)
                created += 1
                created_rows.append(i)
                if item.get("value_missing"):
                    review.append({"part_id": pid, "part_no": item.get("part_no"),
                                   "value": item.get("value"), "missing": item["value_missing"]})
            else:
                linked += 1
            part_ids.append(pid)
            continue
        skipped += 1          # SKIP, MANUAL, or not accepted
        part_ids.append(None)
    return {"created": created, "linked": linked, "skipped": skipped,
            "review": review, "retired": retired, "part_ids": part_ids, "created_rows": created_rows}
