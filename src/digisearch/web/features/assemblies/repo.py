"""Read queries for assemblies (list + single-level BOM detail + where-used)."""

from __future__ import annotations

from ...core.db import Database


def summary(db: Database) -> dict:
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM parts WHERE kind = 'ASSY'").fetchone()[0]
        lines = conn.execute("SELECT COUNT(*) FROM bom_lines").fetchone()[0]
        empty = conn.execute(
            "SELECT COUNT(*) FROM parts p WHERE p.kind = 'ASSY' "
            "AND NOT EXISTS (SELECT 1 FROM bom_lines b WHERE b.parent_id = p.id)"
        ).fetchone()[0]
    return {"assemblies": n, "lines": lines, "empty": empty}


def list_assemblies(db: Database, search: str | None = None) -> list[dict]:
    like = f"%{search}%" if search else None
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT p.id, p.part_no, p.value, p.rev, p.total_qty, p.external_price,
                      (SELECT COUNT(*) FROM bom_lines b WHERE b.parent_id = p.id) AS line_count,
                      (SELECT COUNT(*) FROM bom_lines b WHERE b.child_id = p.id) AS used_in
               FROM parts p
               WHERE p.kind = 'ASSY'
                 AND (:s IS NULL OR p.part_no LIKE :like OR p.value LIKE :like
                      OR p.description LIKE :like)
               ORDER BY p.part_no""",
            {"s": search, "like": like},
        ).fetchall()
    return [dict(r) for r in rows]


def _rolled_unit_cost(conn, part_id: int, seen: set) -> float | None:
    """A part's cost: its own ``unit_cost`` for a component, or the summed cost of its
    children (qty x cost) for an assembly. ``seen`` guards against cyclic BOMs."""
    if part_id in seen:
        return 0.0
    row = conn.execute("SELECT kind, unit_cost FROM parts WHERE id = ?", (part_id,)).fetchone()
    if row is None:
        return None
    if row["kind"] != "ASSY":
        return row["unit_cost"]
    sub_seen = seen | {part_id}
    total = 0.0
    for ln in conn.execute("SELECT child_id, qty_per FROM bom_lines WHERE parent_id = ?", (part_id,)):
        total += (_rolled_unit_cost(conn, ln["child_id"], sub_seen) or 0.0) * (ln["qty_per"] or 0)
    return total


def get_assembly(db: Database, part_id: int) -> dict | None:
    """The assembly part, its direct BOM lines (with costs), and where it's used."""
    with db.connect() as conn:
        head = conn.execute(
            "SELECT * FROM parts WHERE id = ? AND kind = 'ASSY'", (part_id,)
        ).fetchone()
        if head is None:
            return None
        assembly = dict(head)
        lines = [dict(r) for r in conn.execute(
            """SELECT b.id, b.qty_per, b.refdes, b.line_no, b.comments,
                      c.id AS child_id, c.part_no AS child_part_no, c.value AS child_value,
                      c.kind AS child_kind, c.category AS child_category,
                      c.unit_cost AS child_unit_cost
               FROM bom_lines b JOIN parts c ON c.id = b.child_id
               WHERE b.parent_id = ?
               ORDER BY COALESCE(b.line_no, 1e9), b.id""",
            (part_id,),
        )]
        for ln in lines:
            unit = (_rolled_unit_cost(conn, ln["child_id"], set())
                    if ln["child_kind"] == "ASSY" else ln["child_unit_cost"])
            ln["unit_cost"] = unit
            ln["line_cost"] = unit * (ln["qty_per"] or 0) if unit is not None else None
        assembly["lines"] = lines
        assembly["total_cost"] = sum(ln["line_cost"] for ln in lines if ln["line_cost"] is not None)
        assembly["used_in"] = [dict(r) for r in conn.execute(
            """SELECT p.id, p.part_no, p.value, b.qty_per
               FROM bom_lines b JOIN parts p ON p.id = b.parent_id
               WHERE b.child_id = ?
               ORDER BY p.part_no""",
            (part_id,),
        )]
    return assembly


# ---- writes (add / delete BOM lines) ----

def parts_for_picker(db: Database, exclude_id: int) -> list[dict]:
    """All parts that can be added as a child (excludes the assembly itself)."""
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, part_no, value, kind, total_qty FROM parts WHERE id != ? ORDER BY part_no",
            (exclude_id,),
        )]


def create_assembly(db: Database, part: dict) -> int:
    """Create a new assembly (a part with kind=ASSY). Its BOM is built on the detail page."""
    with db.connect() as conn:
        return conn.execute(
            "INSERT INTO parts (part_no, value, description, category, rev, default_build_days, kind) "
            "VALUES (?, ?, ?, ?, ?, ?, 'ASSY')",
            (part["part_no"], part.get("value"), part.get("description"),
             part.get("category"), part.get("rev"), part.get("default_build_days")),
        ).lastrowid


def update_assembly(db: Database, part_id: int, part: dict) -> None:
    """Update an assembly's master fields (kind stays ASSY)."""
    with db.connect() as conn:
        conn.execute(
            "UPDATE parts SET part_no=?, value=?, description=?, category=?, rev=?, "
            "default_build_days=?, updated_at=datetime('now') WHERE id=? AND kind='ASSY'",
            (part["part_no"], part.get("value"), part.get("description"),
             part.get("category"), part.get("rev"), part.get("default_build_days"), part_id),
        )
        conn.commit()


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def convert_to_component(db: Database, part_id: int) -> None:
    """Reclassify an assembly (kind=ASSY) as a plain component (kind=PART) — for fixing parts
    that were mis-entered as assemblies. Refuses if the part still has a BOM or any work order,
    so a real assembly can't be flipped by accident. Its stock, suppliers and where-used links
    (a component can be a BOM child) are all preserved; the assembly-only ``default_build_days``
    is cleared."""
    with db.connect() as conn:
        row = conn.execute("SELECT kind FROM parts WHERE id = ?", (part_id,)).fetchone()
        if row is None:
            raise ValueError("Part not found.")
        if row["kind"] != "ASSY":
            raise ValueError("This part is not an assembly.")

        lines = conn.execute(
            "SELECT COUNT(*) FROM bom_lines WHERE parent_id = ?", (part_id,)
        ).fetchone()[0]
        if lines:
            raise ValueError(
                f"This assembly still has {lines} BOM line(s) — remove them first, then convert.")

        if _table_exists(conn, "work_orders"):
            wos = conn.execute(
                "SELECT COUNT(*) FROM work_orders WHERE assembly_id = ?", (part_id,)
            ).fetchone()[0]
            if wos:
                raise ValueError(
                    f"This assembly is referenced by {wos} work order(s) — it can't be converted.")

        conn.execute(
            "UPDATE parts SET kind = 'PART', default_build_days = NULL, "
            "updated_at = datetime('now') WHERE id = ? AND kind = 'ASSY'",
            (part_id,),
        )
        conn.commit()


def add_bom_line(db: Database, parent_id: int, child_id: int, qty_per: float,
                 refdes: str | None) -> None:
    if child_id == parent_id:
        raise ValueError("An assembly can't contain itself.")
    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM parts WHERE id = ?", (child_id,)).fetchone() is None:
            raise ValueError("Selected component was not found.")
        next_no = conn.execute(
            "SELECT COALESCE(MAX(line_no), 0) + 1 FROM bom_lines WHERE parent_id = ?", (parent_id,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO bom_lines (parent_id, child_id, qty_per, refdes, line_no) "
            "VALUES (?, ?, ?, ?, ?)",
            (parent_id, child_id, qty_per or 1, refdes, next_no),
        )
        conn.commit()


def delete_bom_line(db: Database, parent_id: int, line_id: int) -> None:
    with db.connect() as conn:
        conn.execute(
            "DELETE FROM bom_lines WHERE id = ? AND parent_id = ?", (line_id, parent_id)
        )
        conn.commit()
