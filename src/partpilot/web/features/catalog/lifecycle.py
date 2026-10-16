"""Part lifecycle: retire → (admin) delete, for components and assemblies alike.

A retired part (``parts.retired_at`` not NULL) is hidden from lists and pickers, never suggested for
purchase, never matched by the resolver's own-stock check, and cannot be built or added to a BOM —
but everything that already references it keeps rendering, and its stock may still be despatched
("run out, don't restart"). Retiring applies to the whole version group. Deleting is allowed only
once retired and only when no historical record points at any version in the group; the schema's
ON DELETE CASCADE then removes the part's own BOM lines, offers, tiers and stock rows.
"""

from __future__ import annotations

from ...core.db import Database, table_exists

# label → (table, column). Checked only when the table exists (partial test registries).
# stock_movements cascades in the schema but is a ledger, so it blocks instead of being erased.
_BLOCKERS: tuple[tuple[str, str, str], ...] = (
    ("work orders", "work_orders", "assembly_id"),
    ("work order lines", "work_order_lines", "part_id"),
    ("customer order lines", "customer_order_lines", "part_id"),
    ("allocations", "allocations", "part_id"),
    ("despatch lines", "despatch_lines", "part_id"),
    ("purchase order lines", "purchase_order_lines", "part_id"),
    ("goods receipt lines", "goods_receipt_lines", "part_id"),
    ("stock movements", "stock_movements", "part_id"),
    ("assemblies using it", "bom_lines", "child_id"),
    ("devmgmt variants", "variants", "assembly_id"),
)


def group_ids(conn, part_id: int) -> list[int]:
    """Every version id in the part's group (a legacy un-grouped row is its own group)."""
    return [r["id"] for r in conn.execute(
        "SELECT id FROM parts WHERE COALESCE(version_group_id, id) = "
        "(SELECT COALESCE(version_group_id, id) FROM parts WHERE id = ?)", (part_id,))]


def is_retired(conn, part_id: int) -> bool:
    row = conn.execute("SELECT retired_at FROM parts WHERE id = ?", (part_id,)).fetchone()
    return bool(row and row["retired_at"])


def delete_blockers(conn, part_id: int) -> dict[str, int]:
    """Non-zero reference counts, across every version in the group, keyed by the label shown."""
    ids = group_ids(conn, part_id)
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    out: dict[str, int] = {}
    for label, table, column in _BLOCKERS:
        if not table_exists(conn, table):
            continue
        n = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {column} IN ({marks})", ids).fetchone()[0]
        if n:
            out[label] = n
    return out


def format_blockers(part_no: str, blockers: dict[str, int]) -> str:
    items = [f"used in {n} assemblies" if label == "assemblies using it" else f"{n} {label}"
             for label, n in blockers.items()]
    return f"Cannot delete {part_no}: {', '.join(items)}."


def _set_retired(db: Database, part_id: int, value_sql: str) -> None:
    with db.connect() as conn:
        ids = group_ids(conn, part_id)
        if not ids:
            raise ValueError("Part not found.")
        marks = ",".join("?" for _ in ids)
        conn.execute(
            f"UPDATE parts SET retired_at = {value_sql}, updated_at = datetime('now') "
            f"WHERE id IN ({marks})", ids)
        conn.commit()


def retire_part(db: Database, part_id: int) -> None:
    """Retire every version of the part: hidden, never bought or built again, stock may run out."""
    _set_retired(db, part_id, "datetime('now')")


def unretire_part(db: Database, part_id: int) -> None:
    _set_retired(db, part_id, "NULL")


def delete_part(db: Database, part_id: int) -> None:
    """Delete every version of a retired, unreferenced part. Raises ValueError otherwise."""
    with db.connect() as conn:
        row = conn.execute("SELECT part_no, retired_at FROM parts WHERE id = ?", (part_id,)).fetchone()
        if row is None:
            raise ValueError("Part not found.")
        if not row["retired_at"]:
            raise ValueError(f"Retire {row['part_no']} before deleting it.")
        blockers = delete_blockers(conn, part_id)
        if blockers:
            raise ValueError(format_blockers(row["part_no"], blockers))
        ids = group_ids(conn, part_id)
        marks = ",".join("?" for _ in ids)
        # connect() has foreign_keys ON, so the part's own bom_lines / offers / tiers / stock cascade.
        conn.execute(f"DELETE FROM parts WHERE id IN ({marks})", ids)
        conn.commit()
