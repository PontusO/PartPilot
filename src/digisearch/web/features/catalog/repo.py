"""Read queries for the catalog (parts list + part detail)."""

from __future__ import annotations

import re
from urllib.parse import quote

from ...core.db import Database
from . import pricing

# Distributor part-search URLs, keyed by a normalized supplier-name substring. miniMRP
# doesn't store product URLs, so we link to a keyword search on the supplier part number.
_DISTRIBUTOR_URLS = {
    "digikey": "https://www.digikey.com/en/products/result?keywords={pno}",
    "mouser": "https://www.mouser.com/c/?q={pno}",
    "farnell": "https://se.farnell.com/search?st={pno}",
    "lcsc": "https://www.lcsc.com/search?q={pno}",
    "lscs": "https://www.lcsc.com/search?q={pno}",   # miniMRP spells it "LSCS Electronics"
    "rspro": "https://se.rs-online.com/web/c/?searchTerm={pno}",
    "arrow": "https://www.arrow.com/en/products/search?q={pno}",
}


def distributor_url(supplier_name: str | None, supplier_pno: str | None) -> str | None:
    """A link to the supplier's page for this part, or None for unknown distributors."""
    pno = (supplier_pno or "").strip()
    if not pno:
        return None
    norm = re.sub(r"[^a-z0-9]", "", (supplier_name or "").lower())  # "Digi-Key" -> "digikey"
    for key, template in _DISTRIBUTOR_URLS.items():
        if key in norm:
            return template.format(pno=quote(pno))
    return None

# Picks one supplier row to represent a part in the list: the default if flagged,
# else the lowest-id offer. Avoids row multiplication from a plain join.
_DEFAULT_SUPPLIER = """
    LEFT JOIN part_suppliers ps ON ps.id = (
        SELECT x.id FROM part_suppliers x WHERE x.part_id = p.id
        ORDER BY x.is_default DESC, x.id LIMIT 1
    )
    LEFT JOIN suppliers s ON s.id = ps.supplier_id
"""


def _unit_price(price_per_uom: float | None, qty_per_uom: float | None) -> float | None:
    if price_per_uom is None:
        return None
    return price_per_uom / qty_per_uom if qty_per_uom else price_per_uom


def categories(db: Database) -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT category, COUNT(*) AS n FROM parts WHERE category IS NOT NULL "
            "AND kind != 'ASSY' GROUP BY category ORDER BY category"
        )]


def list_parts(
    db: Database, *, search: str | None = None, category: str | None = None,
    stocked_only: bool = False, limit: int = 100, offset: int = 0,
) -> tuple[list[dict], int]:
    like = f"%{search}%" if search else None
    where = (
        "WHERE p.kind != 'ASSY' "
        "AND (:search IS NULL OR p.part_no LIKE :like OR p.value LIKE :like "
        "OR p.mfr_pno LIKE :like OR p.description LIKE :like) "
        "AND (:category IS NULL OR p.category = :category) "
        "AND (:stocked = 0 OR p.normally_stocked = 1)"
    )
    params = {"search": search, "like": like, "category": category,
              "stocked": 1 if stocked_only else 0}
    with db.connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM parts p {where}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"""SELECT p.id, p.part_no, p.value, p.category, p.kind,
                       p.total_qty, p.total_alloc, p.min_qty, p.unit_cost, p.external_price,
                       p.unlimited_stock, p.normally_stocked,
                       (p.total_qty - p.total_alloc) AS free,
                       s.name AS supplier, ps.supplier_pno AS supplier_pno,
                       ps.price_per_uom AS price_per_uom, ps.qty_per_uom AS qty_per_uom
                FROM parts p {_DEFAULT_SUPPLIER} {where}
                ORDER BY p.category, p.part_no
                LIMIT :limit OFFSET :offset""",
            {**params, "limit": limit, "offset": offset},
        ).fetchall()
    parts = []
    for r in rows:
        d = dict(r)
        d["unit_price"] = _unit_price(d["price_per_uom"], d["qty_per_uom"])
        d["unlimited"] = bool(d["unlimited_stock"])
        d["normally_stocked"] = bool(d["normally_stocked"])
        # An unlimited part never runs out, so it can never be below its reorder point.
        d["below_min"] = (not d["unlimited"]) and d["free"] < (d["min_qty"] or 0)
        parts.append(d)
    return parts, total


def parts_for_supplier(db: Database, supplier_name: str) -> list[dict]:
    """All parts sourced from a supplier (matched by name, case-insensitive)."""
    if not (supplier_name or "").strip():
        return []
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT DISTINCT p.id, p.part_no, p.value, p.category, p.kind,
                      ps.supplier_pno, ps.price_per_uom, ps.qty_per_uom
               FROM parts p
               JOIN part_suppliers ps ON ps.part_id = p.id
               JOIN suppliers s ON s.id = ps.supplier_id
               WHERE lower(s.name) = lower(?)
               ORDER BY p.part_no""",
            (supplier_name.strip(),),
        ).fetchall()
    parts = []
    for r in rows:
        d = dict(r)
        d["unit_price"] = _unit_price(d["price_per_uom"], d["qty_per_uom"])
        d["part_url"] = distributor_url(supplier_name, d["supplier_pno"])
        parts.append(d)
    return parts


def get_part(db: Database, part_id: int) -> dict | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM parts WHERE id = ?", (part_id,)).fetchone()
        if row is None:
            return None
        part = dict(row)
        suppliers = [dict(r) for r in conn.execute(
            """SELECT ps.*, s.name AS supplier_name, s.currency
               FROM part_suppliers ps LEFT JOIN suppliers s ON s.id = ps.supplier_id
               WHERE ps.part_id = ? ORDER BY ps.is_default DESC, ps.id""",
            (part_id,),
        )]
        for s in suppliers:
            s["unit_price"] = _unit_price(s["price_per_uom"], s["qty_per_uom"])
            s["part_url"] = distributor_url(s["supplier_name"], s["supplier_pno"])
            s["cost_tiers"] = [dict(t) for t in conn.execute(
                "SELECT break_qty, unit_price, kind FROM part_supplier_tiers "
                "WHERE part_supplier_id = ? ORDER BY kind, break_qty",
                (s["id"],),
            )]
        sell_tiers = [dict(t) for t in conn.execute(
            "SELECT break_qty, unit_price, source FROM part_price_tiers "
            "WHERE part_id = ? ORDER BY break_qty",
            (part_id,),
        )]
        stock = [dict(r) for r in conn.execute(
            """SELECT pk.*, l.name AS location_name
               FROM part_stock pk LEFT JOIN stock_locations l ON l.id = pk.location_id
               WHERE pk.part_id = ? ORDER BY pk.id""",
            (part_id,),
        )]
    part["suppliers"] = suppliers
    part["sell_tiers"] = sell_tiers
    part["stock"] = stock
    part["unlimited"] = bool(part.get("unlimited_stock"))
    part["normally_stocked"] = bool(part.get("normally_stocked"))
    part["free"] = (part["total_qty"] or 0) - (part["total_alloc"] or 0)
    return part


def summary(db: Database) -> dict:
    with db.connect() as conn:
        n_parts = conn.execute("SELECT COUNT(*) FROM parts WHERE kind != 'ASSY'").fetchone()[0]
        n_cats = conn.execute(
            "SELECT COUNT(DISTINCT category) FROM parts WHERE kind != 'ASSY'"
        ).fetchone()[0]
        n_below = conn.execute(
            "SELECT COUNT(*) FROM parts WHERE kind != 'ASSY' AND unlimited_stock = 0 "
            "AND (total_qty - total_alloc) < min_qty AND min_qty > 0"
        ).fetchone()[0]
        n_stocked = conn.execute(
            "SELECT COUNT(*) FROM parts WHERE kind != 'ASSY' AND normally_stocked = 1"
        ).fetchone()[0]
    return {"parts": n_parts, "categories": n_cats, "below_min": n_below,
            "normally_stocked": n_stocked}


# --- writes (add a component) ---

def suppliers(db: Database) -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT id, name FROM suppliers ORDER BY name")]


def locations(db: Database) -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT id, name FROM stock_locations ORDER BY id")]


def find_part_id_by_mpn(db: Database, mpn: str | None) -> int | None:
    """Look up an existing part by MPN (matches part_no or mfr_pno, case-insensitive)."""
    if not mpn:
        return None
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM parts WHERE lower(part_no) = lower(?) OR lower(mfr_pno) = lower(?) "
            "LIMIT 1",
            (mpn, mpn),
        ).fetchone()
    return row["id"] if row else None


def find_part_by_part_no(db: Database, part_no: str | None) -> dict | None:
    """Look up a part by its canonical ``part_no`` (case-insensitive, exact). Used by the
    webshop sync, which matches on part number only (unlike ``find_part_id_by_mpn``, which
    also matches ``mfr_pno``)."""
    if not (part_no or "").strip():
        return None
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM parts WHERE lower(part_no) = lower(?) ORDER BY id LIMIT 1",
            (part_no.strip(),),
        ).fetchone()
    return dict(row) if row else None


def _get_or_create_supplier(conn, name: str) -> int:
    row = conn.execute("SELECT id FROM suppliers WHERE lower(name) = lower(?)", (name,)).fetchone()
    if row:
        return row["id"]
    return conn.execute("INSERT INTO suppliers (name) VALUES (?)", (name,)).lastrowid


def upsert_supplier(
    db: Database, *, name: str, short_name: str | None = None, url: str | None = None,
    currency: str | None = None, minimrp_id: int | None = None,
) -> int | None:
    """Create/update a ``suppliers`` row so it appears in the part-edit and PO supplier dropdowns.

    The procurement supplier master (``suppliers``) and the Contacts address book
    (``contacts`` WHERE kind='supplier') are otherwise unlinked, so a supplier added in Contacts
    never shows up on parts/POs. Contacts mirrors itself here on save via this function. Matches an
    existing row by ``minimrp_id`` first (the shared miniMRP AddID), then by case-insensitive name.
    Blank incoming fields never overwrite existing values (COALESCE), so editing a contact can't
    erase a currency/short_name a PO already relies on. Returns the supplier id.
    """
    name = (name or "").strip()
    if not name:
        return None
    with db.connect() as conn:
        row = None
        if minimrp_id is not None:
            row = conn.execute(
                "SELECT id FROM suppliers WHERE minimrp_id = ?", (minimrp_id,)).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT id FROM suppliers WHERE lower(name) = lower(?)", (name,)).fetchone()
        if row:
            conn.execute(
                "UPDATE suppliers SET name = ?, short_name = COALESCE(?, short_name), "
                "url = COALESCE(?, url), currency = COALESCE(?, currency), "
                "minimrp_id = COALESCE(minimrp_id, ?) WHERE id = ?",
                (name, short_name, url, currency, minimrp_id, row["id"]),
            )
            sid = row["id"]
        else:
            sid = conn.execute(
                "INSERT INTO suppliers (name, short_name, url, currency, minimrp_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, short_name, url, currency, minimrp_id),
            ).lastrowid
        conn.commit()
        return sid


def _location_id(conn, location_id: int | None) -> int:
    if location_id:
        return location_id
    row = conn.execute("SELECT id FROM stock_locations ORDER BY id LIMIT 1").fetchone()
    if row:
        return row["id"]
    return conn.execute("INSERT INTO stock_locations (name) VALUES ('Main')").lastrowid


def _insert_cost_tiers(conn, part_supplier_id: int, line: dict) -> None:
    """Write auto-captured supplier COST breaks for a ``part_suppliers`` row. ``line`` may carry
    ``cost_tiers`` (cut-tape) and/or ``reel_tiers`` (full-reel), each a list of
    ``{"break_qty", "unit_price"}`` per-piece prices."""
    for kind, key in (("cut", "cost_tiers"), ("reel", "reel_tiers")):
        for t in line.get(key) or []:
            bq, up = t.get("break_qty"), t.get("unit_price")
            if bq is None or up is None:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO part_supplier_tiers "
                "(part_supplier_id, break_qty, unit_price, kind) VALUES (?, ?, ?, ?)",
                (part_supplier_id, bq, up, kind),
            )


def create_part(
    db: Database, *, part: dict, supplier_lines: list[dict], opening: dict | None = None
) -> int:
    """Insert a new component (kind=PART) with its supplier lines and opening stock.

    ``supplier_lines`` items: supplier_name, supplier_pno, unit_price, reel_qty, moq,
    lead_time, is_default. Each line's per-piece ``unit_price`` and ``reel_qty`` are stored
    as price_per_uom = unit_price * reel_qty (matching the imported per-UOM convention).
    ``opening`` (optional): qty, location_id, bin. The part's unit_cost is taken from the
    default supplier's unit price.
    """
    default_unit = next(
        (s["unit_price"] for s in supplier_lines if s.get("is_default")),
        supplier_lines[0]["unit_price"] if supplier_lines else None,
    )
    opening_qty = (opening or {}).get("qty") or 0.0

    with db.connect() as conn:
        part_id = conn.execute(
            """INSERT INTO parts
               (part_no, value, description, category, kind, mfr_name, mfr_pno, rev,
                unit_cost, min_qty, total_qty, notes, unlimited_stock, normally_stocked, markup)
               VALUES (?, ?, ?, ?, 'PART', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (part["part_no"], part.get("value"), part.get("description"), part.get("category"),
             part.get("mfr_name"), part.get("mfr_pno"), part.get("rev"), default_unit,
             part.get("min_qty") or 0, opening_qty, part.get("notes"),
             1 if part.get("unlimited_stock") else 0,
             1 if part.get("normally_stocked") else 0, part.get("markup")),
        ).lastrowid

        for s in supplier_lines:
            qpu = s.get("reel_qty") or 1
            unit = s.get("unit_price")
            price_per_uom = unit * qpu if unit is not None else None
            ps_id = conn.execute(
                """INSERT INTO part_suppliers
                   (part_id, supplier_id, supplier_pno, price_per_uom, qty_per_uom, moq,
                    lead_time, is_default)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (part_id, _get_or_create_supplier(conn, s["supplier_name"]), s.get("supplier_pno"),
                 price_per_uom, qpu, s.get("moq"), s.get("lead_time"), 1 if s.get("is_default") else 0),
            ).lastrowid
            _insert_cost_tiers(conn, ps_id, s)

        for t in part.get("sell_tiers") or []:
            if t.get("break_qty") is None or t.get("unit_price") is None:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO part_price_tiers "
                "(part_id, break_qty, unit_price, source) VALUES (?, ?, ?, 'manual')",
                (part_id, t["break_qty"], t["unit_price"]),
            )

        if opening_qty:
            conn.execute(
                "INSERT INTO part_stock (part_id, location_id, bin, on_hand) VALUES (?, ?, ?, ?)",
                (part_id, _location_id(conn, (opening or {}).get("location_id")),
                 (opening or {}).get("bin"), opening_qty),
            )
        conn.commit()
    return part_id


def update_part(
    db: Database, part_id: int, *, part: dict, supplier_lines: list[dict], stock: dict | None = None
) -> None:
    """Update an existing component. Supplier lines are *reconciled* against the form — an offer
    matched by (supplier, supplier P/N) is updated in place so its ``id`` survives, which keeps the
    auto-captured cost tiers hanging off it (they CASCADE on delete); new offers are inserted and
    removed ones deleted. The primary stock row's on-hand/bin/location is updated and the part's
    total_qty rolled up from all stock rows."""
    default_unit = next(
        (s["unit_price"] for s in supplier_lines if s.get("is_default")),
        supplier_lines[0]["unit_price"] if supplier_lines else None,
    )
    with db.connect() as conn:
        conn.execute(
            """UPDATE parts SET part_no=?, value=?, description=?, category=?, mfr_name=?,
               mfr_pno=?, rev=?, unit_cost=?, min_qty=?, notes=?, unlimited_stock=?,
               normally_stocked=?, markup=?, updated_at=datetime('now')
               WHERE id=?""",
            (part["part_no"], part.get("value"), part.get("description"), part.get("category"),
             part.get("mfr_name"), part.get("mfr_pno"), part.get("rev"), default_unit,
             part.get("min_qty") or 0, part.get("notes"),
             1 if part.get("unlimited_stock") else 0,
             1 if part.get("normally_stocked") else 0, part.get("markup"), part_id),
        )

        # Reconcile supplier offers by (supplier_id, supplier_pno) so matched rows keep their id
        # (and their cascaded cost tiers) instead of being deleted and re-created.
        existing = {}
        for r in conn.execute(
            "SELECT id, supplier_id, supplier_pno FROM part_suppliers WHERE part_id = ?", (part_id,)
        ):
            existing.setdefault((r["supplier_id"], (r["supplier_pno"] or "")), []).append(r["id"])
        kept: set[int] = set()
        for s in supplier_lines:
            qpu = s.get("reel_qty") or 1
            unit = s.get("unit_price")
            price_per_uom = unit * qpu if unit is not None else None
            supplier_id = _get_or_create_supplier(conn, s["supplier_name"])
            pno = s.get("supplier_pno")
            bucket = existing.get((supplier_id, (pno or "")))
            row_id = None
            while bucket:                       # consume a not-yet-reused matching offer
                candidate = bucket.pop(0)
                if candidate not in kept:
                    row_id = candidate
                    break
            if row_id is not None:
                conn.execute(
                    "UPDATE part_suppliers SET price_per_uom=?, qty_per_uom=?, moq=?, "
                    "lead_time=?, is_default=? WHERE id=?",
                    (price_per_uom, qpu, s.get("moq"), s.get("lead_time"),
                     1 if s.get("is_default") else 0, row_id),
                )
                kept.add(row_id)
            else:
                row_id = conn.execute(
                    """INSERT INTO part_suppliers
                       (part_id, supplier_id, supplier_pno, price_per_uom, qty_per_uom, moq,
                        lead_time, is_default)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (part_id, supplier_id, pno, price_per_uom, qpu, s.get("moq"),
                     s.get("lead_time"), 1 if s.get("is_default") else 0),
                ).lastrowid
                kept.add(row_id)
            _insert_cost_tiers(conn, row_id, s)   # no-op unless the line carries captured tiers

        stale = [rid for ids in existing.values() for rid in ids if rid not in kept]
        for rid in stale:                         # dropped offers -> remove (cost tiers CASCADE)
            conn.execute("DELETE FROM part_suppliers WHERE id = ?", (rid,))

        qty = (stock or {}).get("qty")
        row = conn.execute(
            "SELECT id FROM part_stock WHERE part_id = ? ORDER BY id LIMIT 1", (part_id,)
        ).fetchone()
        loc_id = _location_id(conn, (stock or {}).get("location_id"))
        if row is not None:
            conn.execute(
                "UPDATE part_stock SET on_hand=?, bin=?, location_id=? WHERE id=?",
                (qty or 0, (stock or {}).get("bin"), loc_id, row["id"]),
            )
        elif qty:
            conn.execute(
                "INSERT INTO part_stock (part_id, location_id, bin, on_hand) VALUES (?, ?, ?, ?)",
                (part_id, loc_id, (stock or {}).get("bin"), qty),
            )

        conn.execute(
            "UPDATE parts SET total_qty = "
            "(SELECT COALESCE(SUM(on_hand), 0) FROM part_stock WHERE part_id = ?) WHERE id = ?",
            (part_id, part_id),
        )
        conn.commit()


def set_offer_unit_price(conn, part_supplier_id: int, unit_price: float) -> None:
    """Set a supplier offer's price to a per-piece ``unit_price`` (stored per-UOM:
    price_per_uom = unit_price x qty_per_uom). Runs in the caller's transaction (no commit). This is
    the single definition of "write the current/last price onto an offer", used by goods receipt and
    by tier-aware PO pricing."""
    conn.execute("UPDATE part_suppliers SET price_per_uom = ? * qty_per_uom WHERE id = ?",
                 (unit_price, part_supplier_id))


def set_offer_unit_price_db(db: Database, part_supplier_id: int, unit_price: float) -> None:
    """``set_offer_unit_price`` in its own transaction (for callers without an open connection)."""
    with db.connect() as conn:
        set_offer_unit_price(conn, part_supplier_id, unit_price)
        conn.commit()


def replace_cost_tiers(db: Database, part_supplier_id: int,
                       cut_tiers: list[dict], reel_tiers: list[dict]) -> None:
    """Wholesale-replace a supplier offer's captured cost breaks (cut + reel). Used by the
    "refresh from distributor" action, which re-queries Digi-Key/Mouser for the current ladder."""
    with db.connect() as conn:
        conn.execute("DELETE FROM part_supplier_tiers WHERE part_supplier_id = ?", (part_supplier_id,))
        for kind, tiers in (("cut", cut_tiers), ("reel", reel_tiers)):
            for t in tiers or []:
                bq, up = t.get("break_qty"), t.get("unit_price")
                if bq is None or up is None:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO part_supplier_tiers "
                    "(part_supplier_id, break_qty, unit_price, kind) VALUES (?, ?, ?, ?)",
                    (part_supplier_id, bq, up, kind),
                )
        conn.commit()


# --- sell-price tiers ---

def replace_sell_tiers(db: Database, part_id: int, tiers: list[dict]) -> None:
    """Replace the *manual* customer sell tiers for a part with ``tiers`` (each
    ``{"break_qty", "unit_price"}``). Generated (source='markup') tiers are left untouched; a manual
    tier at the same break qty as a generated one supersedes it (manual wins)."""
    with db.connect() as conn:
        conn.execute("DELETE FROM part_price_tiers WHERE part_id = ? AND source = 'manual'", (part_id,))
        for t in tiers:
            bq, up = t.get("break_qty"), t.get("unit_price")
            if bq is None or up is None:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO part_price_tiers "
                "(part_id, break_qty, unit_price, source) VALUES (?, ?, ?, 'manual')",
                (part_id, bq, up),
            )
        conn.commit()


def generate_sell_tiers_from_cost(db: Database, part_id: int, default_markup: float) -> int:
    """(Re)generate source='markup' sell tiers from the default supplier's cut-tape cost breaks,
    each priced at cost x effective markup (the part's own ``markup`` if set, else ``default_markup``).
    Existing manual tiers at the same break qty win and are preserved. Returns the number written."""
    with db.connect() as conn:
        prow = conn.execute("SELECT markup FROM parts WHERE id = ?", (part_id,)).fetchone()
        markup = prow["markup"] if prow is not None and prow["markup"] is not None else default_markup
        ps = conn.execute(
            "SELECT id FROM part_suppliers WHERE part_id = ? ORDER BY is_default DESC, id LIMIT 1",
            (part_id,),
        ).fetchone()
        conn.execute("DELETE FROM part_price_tiers WHERE part_id = ? AND source = 'markup'", (part_id,))
        written = 0
        if ps is not None:
            breaks = conn.execute(
                "SELECT break_qty, unit_price FROM part_supplier_tiers "
                "WHERE part_supplier_id = ? AND kind = 'cut' ORDER BY break_qty",
                (ps["id"],),
            ).fetchall()
            for b in breaks:
                # INSERT OR IGNORE: a manual tier already at this break qty wins.
                cur = conn.execute(
                    "INSERT OR IGNORE INTO part_price_tiers "
                    "(part_id, break_qty, unit_price, source) VALUES (?, ?, ?, 'markup')",
                    (part_id, b["break_qty"], b["unit_price"] * markup),
                )
                written += cur.rowcount
        conn.commit()
    return written


def recalc_sell_tiers_from_purchase(db: Database, part_id: int, anchor_qty: float,
                                    anchor_price: float | None, default_markup: float) -> int:
    """Recompute the generated sell tiers from a purchase, anchored to the price we're paying.

    Sell breaks mirror the default supplier's cut-tape cost tiers; each tier is the cost curve
    *rebased* to the ordered price and marked up::

        sell[i] = anchor_price x (cost[i] / cost[bought_tier]) x markup

    where ``cost[bought_tier]`` is the cost at the tier the ordered qty falls into. When the ordered
    price equals that tier's list cost (the automatic case) this is just ``cost[i] x markup``; a
    negotiated price shifts the whole ladder while keeping the distributor's discount shape. ``markup``
    is the part's own ``markup`` if set, else ``default_markup``. Existing manual (source='manual')
    tiers are preserved and win at their break qty. No-op (returns 0) when the part has no default
    offer, no cost tiers, or the anchor can't be established. Returns the number of tiers written."""
    if anchor_price is None:
        return 0
    with db.connect() as conn:
        prow = conn.execute("SELECT markup FROM parts WHERE id = ?", (part_id,)).fetchone()
        markup = prow["markup"] if prow is not None and prow["markup"] is not None else default_markup
        ps = conn.execute(
            "SELECT id FROM part_suppliers WHERE part_id = ? ORDER BY is_default DESC, id LIMIT 1",
            (part_id,),
        ).fetchone()
        if ps is None:
            return 0
        pairs = [(r["break_qty"], r["unit_price"]) for r in conn.execute(
            "SELECT break_qty, unit_price FROM part_supplier_tiers "
            "WHERE part_supplier_id = ? AND kind = 'cut' ORDER BY break_qty", (ps["id"],))]
        cost_anchor = pricing.price_at(pairs, anchor_qty)
        if not pairs or not cost_anchor:      # nothing to rebase against
            return 0
        conn.execute("DELETE FROM part_price_tiers WHERE part_id = ? AND source = 'markup'", (part_id,))
        written = 0
        for break_qty, cost in pairs:
            sell = anchor_price * (cost / cost_anchor) * markup
            # INSERT OR IGNORE: a manual tier already at this break qty wins.
            cur = conn.execute(
                "INSERT OR IGNORE INTO part_price_tiers "
                "(part_id, break_qty, unit_price, source) VALUES (?, ?, ?, 'markup')",
                (part_id, break_qty, sell),
            )
            written += cur.rowcount
        conn.commit()
    return written
