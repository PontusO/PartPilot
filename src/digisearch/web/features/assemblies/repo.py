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
    """The assembly part, its direct BOM lines, and where it's used. Each line carries the MATERIAL
    cost (``unit_cost``/``line_cost``, what we pay suppliers) and the LOADED cost
    (``loaded_unit``/``loaded_line`` = material × overhead — the true internal build cost). Totals:
    ``total_cost`` (material), ``loaded_total`` (loaded build cost), and ``quote_total`` (the customer
    price = loaded build cost × this product's manufacturing margin)."""
    from ..catalog import pricing
    from ..setup import repo as setup_repo

    overhead = setup_repo.get_default_markup(db)          # material -> loaded
    mfg_default = setup_repo.get_default_mfg_margin(db)   # loaded -> customer quote
    with db.connect() as conn:
        head = conn.execute(
            "SELECT * FROM parts WHERE id = ? AND kind = 'ASSY'", (part_id,)
        ).fetchone()
        if head is None:
            return None
        assembly = dict(head)
        mfg_margin = pricing.effective_mfg_margin(conn, part_id, mfg_default)
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
            qty_per = ln["qty_per"] or 0
            unit = (_rolled_unit_cost(conn, ln["child_id"], set())
                    if ln["child_kind"] == "ASSY" else ln["child_unit_cost"])
            ln["unit_cost"] = unit                       # material, per piece
            ln["line_cost"] = unit * qty_per if unit is not None else None
            loaded_unit = pricing.rolled_sell_price(conn, ln["child_id"], 1, overhead)
            ln["loaded_unit"] = loaded_unit              # loaded (material × overhead), per piece
            ln["loaded_line"] = loaded_unit * qty_per if loaded_unit is not None else None
        assembly["lines"] = lines
        assembly["total_cost"] = sum(ln["line_cost"] for ln in lines if ln["line_cost"] is not None)
        assembly["loaded_total"] = sum(ln["loaded_line"] for ln in lines
                                       if ln["loaded_line"] is not None)
        assembly["quote_total"] = assembly["loaded_total"] * mfg_margin
        assembly["used_in"] = [dict(r) for r in conn.execute(
            """SELECT p.id, p.part_no, p.value, b.qty_per
               FROM bom_lines b JOIN parts p ON p.id = b.parent_id
               WHERE b.child_id = ?
               ORDER BY p.part_no""",
            (part_id,),
        )]
    return assembly


def get_assembly_for_export(
    db: Database, part_id: int, *, build_qty: int = 1, default_markup: float = 1.30,
    default_mfg_margin: float = 1.30,
) -> dict | None:
    """Like :func:`get_assembly` but enriched for the customer-facing xlsx export: each line also
    carries manufacturer, description, the part's best supplier unit price (default supplier if
    flagged, else the cheapest), and — priced at ``build_qty`` — the customer SELL price
    (``sell_unit`` per piece and ``sell_line`` per board = the child's loaded build cost × this
    product's manufacturing margin). ``sell_total`` is the product's per-board customer price at that
    volume. Used only by the export route."""
    from ..catalog import pricing
    with db.connect() as conn:
        head = conn.execute(
            "SELECT * FROM parts WHERE id = ? AND kind = 'ASSY'", (part_id,)
        ).fetchone()
        if head is None:
            return None
        assembly = dict(head)
        # Manufacturing margin is a property of the PRODUCT (this assembly), applied once to the
        # loaded build cost to get the customer price.
        mfg_margin = pricing.effective_mfg_margin(conn, part_id, default_mfg_margin)
        lines = [dict(r) for r in conn.execute(
            """SELECT b.id, b.qty_per, b.refdes, b.line_no, b.comments,
                      c.id AS child_id, c.part_no AS child_part_no, c.value AS child_value,
                      c.description AS child_description, c.kind AS child_kind,
                      c.category AS child_category, c.mfr_name AS child_mfr_name,
                      c.mfr_pno AS child_mfr_pno, c.unit_cost AS child_unit_cost,
                      (SELECT ps.price_per_uom / NULLIF(ps.qty_per_uom, 0)
                         FROM part_suppliers ps
                        WHERE ps.part_id = c.id
                          AND ps.price_per_uom IS NOT NULL
                        ORDER BY ps.is_default DESC,
                                 ps.price_per_uom / NULLIF(ps.qty_per_uom, 0) ASC
                        LIMIT 1) AS child_supplier_price
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
            qty_per = ln["qty_per"] or 0
            # Loaded build cost of this child at the TOTAL quantity the build consumes (qty_per x
            # volume), then × the product's manufacturing margin = the customer sell price.
            loaded_unit = pricing.rolled_sell_price(
                conn, ln["child_id"], qty_per * build_qty, default_markup)
            sell_unit = loaded_unit * mfg_margin if loaded_unit is not None else None
            ln["sell_unit"] = sell_unit
            ln["sell_line"] = sell_unit * qty_per if sell_unit is not None else None
        assembly["lines"] = lines
        assembly["build_qty"] = build_qty
        assembly["total_cost"] = sum(ln["line_cost"] for ln in lines if ln["line_cost"] is not None)
        # The product's per-board sell total is the sum of its direct lines' sell contributions —
        # each `sell_line` already rolled up its child's sub-tree, so no second full-BOM walk needed.
        assembly["sell_total"] = sum(ln["sell_line"] for ln in lines if ln["sell_line"] is not None)
    return assembly


# ---- build-cost estimate (refresh distributor prices for what we'd buy, then roll up) ----

def _explode_to_leaves(conn, part_id: int, build_qty: float) -> dict:
    """Total quantity of each LEAF component consumed to build ``build_qty`` of ``part_id`` — a leaf
    used in several places is summed. Cycle-guarded (mirrors ``_rolled_unit_cost``'s traversal)."""
    needs: dict = {}

    def walk(pid, qty, path):
        if pid in path:
            return
        row = conn.execute("SELECT kind FROM parts WHERE id = ?", (pid,)).fetchone()
        if row is None:
            return
        if row["kind"] != "ASSY":
            needs[pid] = needs.get(pid, 0.0) + qty
            return
        for c in conn.execute("SELECT child_id, qty_per FROM bom_lines WHERE parent_id = ?", (pid,)):
            walk(c["child_id"], qty * (c["qty_per"] or 0), path | {pid})

    walk(part_id, build_qty, frozenset())
    return needs


def refresh_bom_for_estimate(db: Database, part_id: int, build_qty: int) -> dict:
    """Re-query distributor cost tiers for the BOM's leaf components that are SHORT for this build
    (free stock < needed). Components with sufficient free stock (or unlimited) are left alone — we
    won't buy them — and non-distributor leaves are reported as skipped. Persists the refreshed cost
    tiers but NOT ``parts.unit_cost``. Returns a human-readable summary."""
    from ..catalog import cost_refresh

    build_qty = max(1, build_qty)
    clients = cost_refresh.build_clients()
    with db.connect() as conn:
        needs = _explode_to_leaves(conn, part_id, build_qty)
        info = {}
        for leaf_id in needs:
            r = conn.execute(
                "SELECT part_no, total_qty, total_alloc, unlimited_stock FROM parts WHERE id = ?",
                (leaf_id,)).fetchone()
            info[leaf_id] = dict(r) if r else None

    refreshed, in_stock, skipped, errors = [], [], [], []
    for leaf_id, needed in needs.items():
        r = info.get(leaf_id)
        if r is None:
            continue
        label = r["part_no"] or f"#{leaf_id}"
        if r["unlimited_stock"]:
            skipped.append(f"{label}: unlimited stock")
            continue
        free = (r["total_qty"] or 0) - (r["total_alloc"] or 0)
        if free >= needed:
            in_stock.append(f"{label}: {free:g} on hand ≥ {needed:g} needed")
            continue
        res = cost_refresh.refresh_cost_tiers(db, leaf_id, clients)   # refreshes cost tiers only
        if res["updated"]:
            refreshed.append(f"{label} ({needed:g} needed): {'; '.join(res['updated'])}")
        elif res["errors"]:
            errors.append(f"{label}: {'; '.join(res['errors'])}")
        else:
            skipped.append(f"{label}: {'; '.join(res['skipped']) or 'no distributor price'}")
    return {"refreshed": refreshed, "in_stock": in_stock, "skipped": skipped, "errors": errors}


def estimate_bom_cost(db: Database, part_id: int, build_qty: int,
                      default_markup: float = 1.30, default_mfg_margin: float = 1.30) -> dict:
    """Transient build estimate at ``build_qty``, the full cost ladder at that volume:
      - ``material`` — supplier cost (``rolled_cost_at``, from cost tiers -> unit_cost),
      - ``loaded``   — material × overhead (``rolled_sell_price``), the true internal build cost,
      - ``quote``    — loaded × this product's manufacturing margin, the customer price.
    Reads only; never writes ``parts.unit_cost``. Returns totals plus per-line
    ``{bom_line_id: {"material", "loaded"}}``."""
    from ..catalog import pricing

    build_qty = max(1, build_qty)
    per_line: dict = {}
    material_total = loaded_total = 0.0
    with db.connect() as conn:
        mfg_margin = pricing.effective_mfg_margin(conn, part_id, default_mfg_margin)
        for ln in conn.execute(
            "SELECT id, child_id, qty_per FROM bom_lines WHERE parent_id = ?", (part_id,)):
            qty_per = ln["qty_per"] or 0
            need = qty_per * build_qty
            mat_unit = pricing.rolled_cost_at(conn, ln["child_id"], need)
            loaded_unit = pricing.rolled_sell_price(conn, ln["child_id"], need, default_markup)
            mat_line = mat_unit * qty_per if mat_unit is not None else None
            loaded_line = loaded_unit * qty_per if loaded_unit is not None else None
            per_line[ln["id"]] = {"material": mat_line, "loaded": loaded_line}
            if mat_line is not None:
                material_total += mat_line
            if loaded_line is not None:
                loaded_total += loaded_line
    return {"build_qty": build_qty, "material_total": material_total,
            "loaded_total": loaded_total, "quote_total": loaded_total * mfg_margin,
            "per_line": per_line}


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
            "INSERT INTO parts (part_no, value, description, category, rev, default_build_days, "
            "mfg_margin, kind) VALUES (?, ?, ?, ?, ?, ?, ?, 'ASSY')",
            (part["part_no"], part.get("value"), part.get("description"),
             part.get("category"), part.get("rev"), part.get("default_build_days"),
             part.get("mfg_margin")),
        ).lastrowid


def update_assembly(db: Database, part_id: int, part: dict) -> None:
    """Update an assembly's master fields (kind stays ASSY)."""
    with db.connect() as conn:
        conn.execute(
            "UPDATE parts SET part_no=?, value=?, description=?, category=?, rev=?, "
            "default_build_days=?, mfg_margin=?, updated_at=datetime('now') WHERE id=? AND kind='ASSY'",
            (part["part_no"], part.get("value"), part.get("description"),
             part.get("category"), part.get("rev"), part.get("default_build_days"),
             part.get("mfg_margin"), part_id),
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


def update_bom_line(db: Database, parent_id: int, line_id: int, qty_per: float,
                    refdes: str | None) -> None:
    """Update a single BOM line's quantity and reference designators (scoped to its parent)."""
    with db.connect() as conn:
        conn.execute(
            "UPDATE bom_lines SET qty_per = ?, refdes = ? WHERE id = ? AND parent_id = ?",
            (qty_per or 1, refdes, line_id, parent_id),
        )
        conn.commit()


def delete_bom_line(db: Database, parent_id: int, line_id: int) -> None:
    with db.connect() as conn:
        conn.execute(
            "DELETE FROM bom_lines WHERE id = ? AND parent_id = ?", (line_id, parent_id)
        )
        conn.commit()
