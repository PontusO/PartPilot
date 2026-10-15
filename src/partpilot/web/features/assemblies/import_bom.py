"""Import a CSV BOM onto an assembly, reusing the shared parts-import engine.

The part-level work (classify resolved lines, create accepted parts) lives in
``catalog/import_parts.py`` so the Setup "Import parts" tool shares it. This module adds the
assembly step: one BOM line per row that now has a catalog part.
"""

from __future__ import annotations

from ...core.db import Database
from ..catalog.import_parts import (  # noqa: F401  (re-exported for callers/tests)
    IN_INVENTORY,
    MANUAL,
    NEW,
    SKIP,
    UNRESOLVED,
    apply_parts_plan,
    build_import_plan,
)
from . import repo as asm_repo


def apply_import_plan(
    db: Database, assembly_id: int, plan: list[dict], accepted: set[int]
) -> dict:
    """Create accepted parts and add a BOM line per row that has a part.

    Returns the counts plus ``review``: the parts we just created whose iLabs value notation is
    incomplete (a distributor parametric was missing), so the caller can list them with edit links.
    """
    res = apply_parts_plan(db, plan, accepted)
    lines_added = 0
    for item, child_id in zip(plan, res["part_ids"]):
        if child_id is None:
            continue
        asm_repo.add_bom_line(db, assembly_id, child_id, item.get("qty") or 1, item.get("refdes"))
        lines_added += 1
    return {"created": res["created"], "lines_added": lines_added,
            "skipped": res["skipped"], "review": res["review"]}
