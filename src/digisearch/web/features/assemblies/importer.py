"""Bulk load of a BOM tree into ``bom_lines``, over already-parsed ``usedin`` row dicts.

``import_bom_rows`` maps ParentID/ChildID via a caller-supplied ``{minimrp_id: parts.id}`` map and
upserts keyed on ``minimrp_id`` (the legacy source id, kept purely as an idempotency key). It reads
nothing external. (The miniMRP mdb reader that used to feed it was removed with the decommission.)
"""

from __future__ import annotations

from ...core.db import Database


def _f(x, default: float = 0.0) -> float:
    try:
        return float(x) if x not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _i(x) -> int | None:
    try:
        return int(float(x)) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _s(x) -> str | None:
    s = (x or "").strip()
    return s or None


def import_bom_rows(db: Database, *, parts_map: dict[int, int], usedin: list[dict]) -> dict[str, int]:
    """Upsert tblusedin rows into bom_lines using a {minimrp_id: parts.id} map."""
    n = 0
    skipped = 0
    with db.connect() as conn:
        for r in usedin:
            parent = parts_map.get(_i(r.get("ParentID")))
            child = parts_map.get(_i(r.get("ChildID")))
            if parent is None or child is None:
                skipped += 1
                continue
            conn.execute(
                """INSERT INTO bom_lines
                   (parent_id, child_id, qty_per, refdes, line_no, comments, minimrp_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(minimrp_id) DO UPDATE SET
                     parent_id=excluded.parent_id, child_id=excluded.child_id,
                     qty_per=excluded.qty_per, refdes=excluded.refdes,
                     line_no=excluded.line_no, comments=excluded.comments""",
                (parent, child, _f(r.get("QtyPer")) or 1, _s(r.get("RefText")),
                 _i(r.get("LineItemNo")), _s(r.get("Comments")), _i(r.get("AutoID"))),
            )
            n += 1
        conn.commit()
    return {"bom_lines": n, "skipped": skipped}


