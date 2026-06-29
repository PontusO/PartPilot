"""Import a CSV BOM onto an assembly, reusing the purchasing tool's resolution.

Flow: resolve the BOM with ``purchasing.service.resolve_bom_file`` (same engine the
purchasing tool uses, so changes propagate), classify each line against the catalog, let
the user accept new parts on a review screen, then create the accepted parts and add a BOM
line per row. The intermediate "plan" is a plain JSON-serializable list so it can be saved
between the review POST and the apply POST.
"""

from __future__ import annotations

from digisearch.models import CompType, ResolvedLine, Status
from digisearch.spec.ilabs_value import build_value_string

from ...core.db import Database
from ..catalog import repo as catalog_repo
from ..purchasing.service import ResolvedRun
from . import repo as asm_repo

# Per-line status on the review screen.
IN_INVENTORY = "in_inventory"   # already a catalog part -> auto-linked
NEW = "new"                     # resolved on Digi-Key/Mouser, not in catalog -> offer to create
UNRESOLVED = "unresolved"       # searched, no match, but has a value/MPN -> offer a bare create
MANUAL = "manual"               # under-specified (no value/MPN) -> needs a human, can't import
SKIP = "skip"                   # DNP / non-orderable -> not added


def _target_mpn(line: ResolvedLine) -> str | None:
    if line.chosen and line.chosen.mpn:
        return line.chosen.mpn
    return line.stock_match  # IN_STOCK lines carry the matched stock MPN


def _category(line: ResolvedLine) -> str | None:
    if line.spec and line.spec.comp_type and line.spec.comp_type != CompType.OTHER:
        return line.spec.comp_type.value.upper()
    return None


def _value_for(line: ResolvedLine) -> tuple[str | None, list[str]]:
    """The part's ``value``: the iLabs slash notation for a passive (built from the parsed spec
    plus the distributor's parametrics), else the raw BOM value for ICs/connectors/etc. The
    second element lists spec fields we couldn't fill, so the importer can flag it for review.
    """
    built = build_value_string(line.spec, line.chosen)
    if built is not None:
        return built.value or None, built.missing
    return line.line.value or None, []   # non-passive: keep the raw BOM value, nothing to flag


def build_import_plan(db: Database, run: ResolvedRun) -> list[dict]:
    """Turn resolved lines into a review plan (one JSON-serializable dict per BOM line)."""
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
        value, missing = _value_for(ln)
        if existing_id is not None:
            item.update(status=IN_INVENTORY, part_id=existing_id, part_no=mpn)
        elif ln.chosen is not None:
            c = ln.chosen
            item.update(
                status=NEW, part_no=c.mpn, value=value, value_missing=missing,
                category=_category(ln), mfr_name=c.manufacturer, mfr_pno=c.mpn,
                supplier_name=c.supplier, supplier_pno=c.dk_part_number, unit_cost=c.unit_price,
                reel_qty=c.reel_qty or 1,
                supplier_label=f"{c.supplier or ''} {c.dk_part_number or ''}".strip(),
            )
        else:
            item.update(
                status=UNRESOLVED, part_no=(ln.line.value or ln.line.device or "").strip() or None,
                value=value, value_missing=missing, category=_category(ln),
            )
        plan.append(item)
    return plan


def _create_part(db: Database, item: dict) -> int:
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
        }]
    return catalog_repo.create_part(db, part=part, supplier_lines=supplier_lines, opening=None)


def apply_import_plan(
    db: Database, assembly_id: int, plan: list[dict], accepted: set[int]
) -> dict:
    """Create accepted parts and add a BOM line per applicable row.

    Returns the counts plus ``review``: the parts we just created whose iLabs value notation is
    incomplete (a distributor parametric was missing), so the caller can list them with edit links.
    """
    created = lines_added = skipped = 0
    review: list[dict] = []
    for i, item in enumerate(plan):
        status = item.get("status")
        if status == IN_INVENTORY:
            child_id = item["part_id"]
        elif status in (NEW, UNRESOLVED):
            if i not in accepted:
                skipped += 1
                continue
            # Re-use an existing catalog part if its MPN already landed (avoids duplicates).
            child_id = catalog_repo.find_part_id_by_mpn(db, item.get("part_no"))
            if child_id is None:
                child_id = _create_part(db, item)
                created += 1
                if item.get("value_missing"):
                    review.append({"part_id": child_id, "part_no": item.get("part_no"),
                                   "value": item.get("value"), "missing": item["value_missing"]})
        else:  # SKIP / unknown
            skipped += 1
            continue
        asm_repo.add_bom_line(db, assembly_id, child_id, item.get("qty") or 1, item.get("refdes"))
        lines_added += 1
    return {"created": created, "lines_added": lines_added, "skipped": skipped, "review": review}
