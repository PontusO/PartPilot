"""Quantity-break (tiered) pricing helpers — pure DB reads, no feature/config imports.

Two tier flavours (catalog migration v14):
  * COST tiers  (``part_supplier_tiers``) — what we pay a supplier at a break qty, auto-captured
    from Digi-Key/Mouser price breaks.
  * SELL tiers  (``part_price_tiers``)     — what we charge the customer at a component-qty break.

Tier selection mirrors the CLI engine's ``Candidate.price_at`` (src/digisearch/models.py): pick the
highest break quantity <= the quantity in play. The quantity in play for a component is the *total*
number consumed by the build (``qty_per`` x volume, cumulative down the BOM), which is what lines up
with how we buy reels.

The ``default_markup`` threaded into these helpers is the app setting ``pricing.default_markup``; a
part's own ``parts.markup`` overrides it when set. Callers fetch the default once and pass it down,
keeping this module unit-testable without the settings store.
"""

from __future__ import annotations


def price_at(tiers: list[tuple[float, float]], qty: float) -> float | None:
    """Unit price for the highest break quantity <= ``qty``.

    ``tiers`` is ``[(break_qty, unit_price), ...]`` ascending by break_qty. Below the smallest break
    we fall back to that smallest-break price (you still pay the low-qty rate); an empty list is None.
    """
    applicable = [p for (bq, p) in tiers if bq <= qty]
    if applicable:
        return applicable[-1]
    return tiers[0][1] if tiers else None


def load_sell_tiers(conn, part_id: int) -> list[tuple[float, float]]:
    rows = conn.execute(
        "SELECT break_qty, unit_price FROM part_price_tiers WHERE part_id = ? ORDER BY break_qty",
        (part_id,),
    ).fetchall()
    return [(r["break_qty"], r["unit_price"]) for r in rows]


def load_cost_tiers(conn, part_supplier_id: int, kind: str = "cut") -> list[tuple[float, float]]:
    rows = conn.execute(
        "SELECT break_qty, unit_price FROM part_supplier_tiers "
        "WHERE part_supplier_id = ? AND kind = ? ORDER BY break_qty",
        (part_supplier_id, kind),
    ).fetchall()
    return [(r["break_qty"], r["unit_price"]) for r in rows]


def _default_supplier_id(conn, part_id: int) -> int | None:
    row = conn.execute(
        "SELECT id FROM part_suppliers WHERE part_id = ? ORDER BY is_default DESC, id LIMIT 1",
        (part_id,),
    ).fetchone()
    return row["id"] if row else None


def _effective_markup(conn, part_id: int, default_markup: float) -> float:
    row = conn.execute("SELECT markup FROM parts WHERE id = ?", (part_id,)).fetchone()
    if row is not None and row["markup"] is not None:
        return row["markup"]
    return default_markup


def leaf_sell_unit(conn, part_id: int, qty: float, default_markup: float) -> float | None:
    """Per-piece SELL price of a single (non-assembly) part at ``qty``.

    Sell tiers win when present. Otherwise the part's cost at ``qty`` (default-supplier cost tier,
    else the flat ``parts.unit_cost``) times its effective markup (per-part override, else default).
    """
    sell = load_sell_tiers(conn, part_id)
    if sell:
        return price_at(sell, qty)

    markup = _effective_markup(conn, part_id, default_markup)
    ps_id = _default_supplier_id(conn, part_id)
    cost = price_at(load_cost_tiers(conn, ps_id), qty) if ps_id is not None else None
    if cost is None:
        row = conn.execute("SELECT unit_cost FROM parts WHERE id = ?", (part_id,)).fetchone()
        cost = row["unit_cost"] if row is not None else None
    return cost * markup if cost is not None else None


def rolled_sell_price(conn, part_id: int, qty_needed: float, default_markup: float,
                      seen: frozenset = frozenset()) -> float | None:
    """Per-unit SELL price of ``part_id`` when ``qty_needed`` of it are being made/ordered.

    A leaf part is priced by ``leaf_sell_unit``. An assembly rolls up its BOM: each child
    contributes ``rolled_sell_price(child, qty_needed * qty_per) * qty_per`` — the child's tier is
    chosen by the *total* child quantity the build consumes. Cycle-guarded like
    ``customer_orders._all_prices``.
    """
    row = conn.execute("SELECT kind FROM parts WHERE id = ?", (part_id,)).fetchone()
    if row is None:
        return None
    if row["kind"] != "ASSY":
        return leaf_sell_unit(conn, part_id, qty_needed, default_markup)
    if part_id in seen:            # cyclic BOM guard
        return 0.0
    children = conn.execute(
        "SELECT child_id, qty_per FROM bom_lines WHERE parent_id = ?", (part_id,)
    ).fetchall()
    total = 0.0
    for c in children:
        qty_per = c["qty_per"] or 0
        child_unit = rolled_sell_price(conn, c["child_id"], qty_needed * qty_per,
                                       default_markup, seen | {part_id})
        total += (child_unit or 0.0) * qty_per
    return total


def effective_mfg_margin(conn, part_id: int, default_mfg: float) -> float:
    """The product's manufacturing (profit) margin — its own ``parts.mfg_margin`` if set, else the
    ``default_mfg`` (app setting)."""
    row = conn.execute("SELECT mfg_margin FROM parts WHERE id = ?", (part_id,)).fetchone()
    if row is not None and row["mfg_margin"] is not None:
        return row["mfg_margin"]
    return default_mfg


def product_sell_price(conn, part_id: int, qty: float, overhead_default: float,
                       mfg_default: float) -> float | None:
    """Customer price for a finished product at ``qty``: its LOADED build cost
    (``rolled_sell_price`` = material × overhead, rolled up the BOM) × the product's MANUFACTURING
    margin. The margin (profit) is applied once, HERE — never inside ``rolled_sell_price`` — so a
    sub-assembly consumed internally never compounds it."""
    loaded = rolled_sell_price(conn, part_id, qty, overhead_default)
    if loaded is None:
        return None
    return loaded * effective_mfg_margin(conn, part_id, mfg_default)


def leaf_cost_at(conn, part_id: int, qty: float) -> float | None:
    """Per-piece COST of a single (non-assembly) part at ``qty``: the default supplier's cut cost tier
    at ``qty`` (``price_at(load_cost_tiers(...))``), else the flat ``parts.unit_cost``. No markup —
    this is the cost basis, used for a build-cost estimate (cf. ``leaf_sell_unit``)."""
    ps_id = _default_supplier_id(conn, part_id)
    cost = price_at(load_cost_tiers(conn, ps_id), qty) if ps_id is not None else None
    if cost is None:
        row = conn.execute("SELECT unit_cost FROM parts WHERE id = ?", (part_id,)).fetchone()
        cost = row["unit_cost"] if row is not None else None
    return cost


def rolled_cost_at(conn, part_id: int, qty_needed: float,
                   seen: frozenset = frozenset()) -> float | None:
    """Per-unit COST of ``part_id`` when ``qty_needed`` are being built — the cost analogue of
    ``rolled_sell_price``. Leaf → ``leaf_cost_at``; assembly → Σ children
    ``rolled_cost_at(child, qty_needed * qty_per) * qty_per`` (each child's cost tier chosen by the
    total quantity the build consumes). Cycle-guarded."""
    row = conn.execute("SELECT kind FROM parts WHERE id = ?", (part_id,)).fetchone()
    if row is None:
        return None
    if row["kind"] != "ASSY":
        return leaf_cost_at(conn, part_id, qty_needed)
    if part_id in seen:            # cyclic BOM guard
        return 0.0
    children = conn.execute(
        "SELECT child_id, qty_per FROM bom_lines WHERE parent_id = ?", (part_id,)
    ).fetchall()
    total = 0.0
    for c in children:
        qty_per = c["qty_per"] or 0
        child_unit = rolled_cost_at(conn, c["child_id"], qty_needed * qty_per, seen | {part_id})
        total += (child_unit or 0.0) * qty_per
    return total
