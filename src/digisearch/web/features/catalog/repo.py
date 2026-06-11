"""Read queries for the catalog (parts list + part detail)."""

from __future__ import annotations

import re
from urllib.parse import quote

from ...core.db import Database

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
    limit: int = 100, offset: int = 0,
) -> tuple[list[dict], int]:
    like = f"%{search}%" if search else None
    where = (
        "WHERE p.kind != 'ASSY' "
        "AND (:search IS NULL OR p.part_no LIKE :like OR p.value LIKE :like "
        "OR p.mfr_pno LIKE :like OR p.description LIKE :like) "
        "AND (:category IS NULL OR p.category = :category)"
    )
    params = {"search": search, "like": like, "category": category}
    with db.connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM parts p {where}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"""SELECT p.id, p.part_no, p.value, p.category, p.kind,
                       p.total_qty, p.total_alloc, p.min_qty, p.unit_cost,
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
        d["below_min"] = d["free"] < (d["min_qty"] or 0)
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
        stock = [dict(r) for r in conn.execute(
            """SELECT pk.*, l.name AS location_name
               FROM part_stock pk LEFT JOIN stock_locations l ON l.id = pk.location_id
               WHERE pk.part_id = ? ORDER BY pk.id""",
            (part_id,),
        )]
    part["suppliers"] = suppliers
    part["stock"] = stock
    part["free"] = (part["total_qty"] or 0) - (part["total_alloc"] or 0)
    return part


def summary(db: Database) -> dict:
    with db.connect() as conn:
        n_parts = conn.execute("SELECT COUNT(*) FROM parts WHERE kind != 'ASSY'").fetchone()[0]
        n_cats = conn.execute(
            "SELECT COUNT(DISTINCT category) FROM parts WHERE kind != 'ASSY'"
        ).fetchone()[0]
        n_below = conn.execute(
            "SELECT COUNT(*) FROM parts WHERE kind != 'ASSY' "
            "AND (total_qty - total_alloc) < min_qty AND min_qty > 0"
        ).fetchone()[0]
    return {"parts": n_parts, "categories": n_cats, "below_min": n_below}


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


def _get_or_create_supplier(conn, name: str) -> int:
    row = conn.execute("SELECT id FROM suppliers WHERE lower(name) = lower(?)", (name,)).fetchone()
    if row:
        return row["id"]
    return conn.execute("INSERT INTO suppliers (name) VALUES (?)", (name,)).lastrowid


def _location_id(conn, location_id: int | None) -> int:
    if location_id:
        return location_id
    row = conn.execute("SELECT id FROM stock_locations ORDER BY id LIMIT 1").fetchone()
    if row:
        return row["id"]
    return conn.execute("INSERT INTO stock_locations (name) VALUES ('Main')").lastrowid


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
                unit_cost, min_qty, total_qty, notes)
               VALUES (?, ?, ?, ?, 'PART', ?, ?, ?, ?, ?, ?, ?)""",
            (part["part_no"], part.get("value"), part.get("description"), part.get("category"),
             part.get("mfr_name"), part.get("mfr_pno"), part.get("rev"), default_unit,
             part.get("min_qty") or 0, opening_qty, part.get("notes")),
        ).lastrowid

        for s in supplier_lines:
            qpu = s.get("reel_qty") or 1
            unit = s.get("unit_price")
            price_per_uom = unit * qpu if unit is not None else None
            conn.execute(
                """INSERT INTO part_suppliers
                   (part_id, supplier_id, supplier_pno, price_per_uom, qty_per_uom, moq,
                    lead_time, is_default)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (part_id, _get_or_create_supplier(conn, s["supplier_name"]), s.get("supplier_pno"),
                 price_per_uom, qpu, s.get("moq"), s.get("lead_time"), 1 if s.get("is_default") else 0),
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
    """Update an existing component. Supplier lines are fully redefined by the form
    (wiped and re-created); the primary stock row's on-hand/bin/location is updated and
    the part's total_qty rolled up from all stock rows."""
    default_unit = next(
        (s["unit_price"] for s in supplier_lines if s.get("is_default")),
        supplier_lines[0]["unit_price"] if supplier_lines else None,
    )
    with db.connect() as conn:
        conn.execute(
            """UPDATE parts SET part_no=?, value=?, description=?, category=?, mfr_name=?,
               mfr_pno=?, rev=?, unit_cost=?, min_qty=?, notes=?, updated_at=datetime('now')
               WHERE id=?""",
            (part["part_no"], part.get("value"), part.get("description"), part.get("category"),
             part.get("mfr_name"), part.get("mfr_pno"), part.get("rev"), default_unit,
             part.get("min_qty") or 0, part.get("notes"), part_id),
        )

        conn.execute("DELETE FROM part_suppliers WHERE part_id = ?", (part_id,))
        for s in supplier_lines:
            qpu = s.get("reel_qty") or 1
            unit = s.get("unit_price")
            price_per_uom = unit * qpu if unit is not None else None
            conn.execute(
                """INSERT INTO part_suppliers
                   (part_id, supplier_id, supplier_pno, price_per_uom, qty_per_uom, moq,
                    lead_time, is_default)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (part_id, _get_or_create_supplier(conn, s["supplier_name"]), s.get("supplier_pno"),
                 price_per_uom, qpu, s.get("moq"), s.get("lead_time"), 1 if s.get("is_default") else 0),
            )

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
