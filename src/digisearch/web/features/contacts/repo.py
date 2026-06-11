"""Queries + writes for the contacts address book."""

from __future__ import annotations

from ...core.db import Database

KINDS = ("supplier", "customer", "other")

_FIELDS = ("kind", "name", "short_name", "contact", "email", "phone", "phone2", "fax",
           "address", "postcode", "country", "website", "currency", "discount", "notes")

# Structured address columns written from the address form.
_ADDRESS_FIELDS = ("label", "company", "contact", "line1", "line2", "city", "region",
                   "postcode", "country", "phone", "email", "is_delivery", "is_invoice")


def summary(db: Database) -> dict:
    with db.connect() as conn:
        rows = dict(conn.execute(
            "SELECT kind, COUNT(*) FROM contacts GROUP BY kind").fetchall())
        total = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
    return {"total": total, "suppliers": rows.get("supplier", 0),
            "customers": rows.get("customer", 0), "other": rows.get("other", 0)}


def list_contacts(db: Database, kind: str | None = None, search: str | None = None) -> list[dict]:
    like = f"%{search}%" if search else None
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT id, kind, name, short_name, contact, email, phone, website
               FROM contacts
               WHERE (:kind IS NULL OR kind = :kind)
                 AND (:search IS NULL OR name LIKE :like OR short_name LIKE :like
                      OR contact LIKE :like OR email LIKE :like)
               ORDER BY name""",
            {"kind": kind, "search": search, "like": like},
        )]


def get_contact(db: Database, contact_id: int) -> dict | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    return dict(row) if row else None


def create_contact(db: Database, data: dict) -> int:
    cols = ", ".join(_FIELDS)
    placeholders = ", ".join("?" for _ in _FIELDS)
    with db.connect() as conn:
        cur = conn.execute(
            f"INSERT INTO contacts ({cols}) VALUES ({placeholders})",
            tuple(data.get(f) for f in _FIELDS),
        )
        conn.commit()
        return cur.lastrowid


def update_contact(db: Database, contact_id: int, data: dict) -> None:
    assignments = ", ".join(f"{f} = ?" for f in _FIELDS)
    with db.connect() as conn:
        conn.execute(
            f"UPDATE contacts SET {assignments}, updated_at = datetime('now') WHERE id = ?",
            (*[data.get(f) for f in _FIELDS], contact_id),
        )
        conn.commit()


# ---- structured addresses (delivery / invoice) ----

def list_addresses(db: Database, contact_id: int) -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM contact_addresses WHERE contact_id = ? ORDER BY id", (contact_id,))]


def get_address(db: Database, address_id: int) -> dict | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM contact_addresses WHERE id = ?", (address_id,)).fetchone()
    return dict(row) if row else None


def addresses_for(db: Database, contact_id: int, usage: str) -> list[dict]:
    """A contact's addresses usable for ``usage`` ('delivery'|'invoice'), default first."""
    flag = "is_delivery" if usage == "delivery" else "is_invoice"
    default = "is_default_delivery" if usage == "delivery" else "is_default_invoice"
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT * FROM contact_addresses WHERE contact_id = ? AND {flag} = 1 "
            f"ORDER BY {default} DESC, id", (contact_id,))]


def default_delivery_address(db: Database, contact_id: int) -> dict | None:
    return _default_address(db, contact_id, "is_default_delivery", "is_delivery")


def default_invoice_address(db: Database, contact_id: int) -> dict | None:
    return _default_address(db, contact_id, "is_default_invoice", "is_invoice")


def _default_address(db: Database, contact_id: int, default_col: str, usage_col: str) -> dict | None:
    with db.connect() as conn:
        row = conn.execute(
            f"SELECT * FROM contact_addresses WHERE contact_id = ? AND {usage_col} = 1 "
            f"ORDER BY {default_col} DESC, id LIMIT 1", (contact_id,)).fetchone()
    return dict(row) if row else None


_ADDRESS_FLAGS = ("is_delivery", "is_invoice")


def _address_value(field: str, data: dict):
    """Usage flags are NOT NULL → coerce to 0/1; a row flagged default is implicitly usable."""
    if field in _ADDRESS_FLAGS:
        default_key = "is_default_delivery" if field == "is_delivery" else "is_default_invoice"
        return 1 if (data.get(field) or data.get(default_key)) else 0
    return data.get(field)


def create_address(db: Database, contact_id: int, data: dict) -> int:
    cols = ", ".join(_ADDRESS_FIELDS)
    placeholders = ", ".join("?" for _ in _ADDRESS_FIELDS)
    with db.connect() as conn:
        aid = conn.execute(
            f"INSERT INTO contact_addresses (contact_id, {cols}) VALUES (?, {placeholders})",
            (contact_id, *[_address_value(f, data) for f in _ADDRESS_FIELDS]),
        ).lastrowid
        _apply_default_flags(conn, contact_id, aid, data)
        conn.commit()
        return aid


def update_address(db: Database, address_id: int, data: dict) -> None:
    assignments = ", ".join(f"{f} = ?" for f in _ADDRESS_FIELDS)
    with db.connect() as conn:
        row = conn.execute(
            "SELECT contact_id FROM contact_addresses WHERE id = ?", (address_id,)).fetchone()
        if row is None:
            return
        conn.execute(
            f"UPDATE contact_addresses SET {assignments}, updated_at = datetime('now') WHERE id = ?",
            (*[_address_value(f, data) for f in _ADDRESS_FIELDS], address_id),
        )
        _apply_default_flags(conn, row["contact_id"], address_id, data)
        conn.commit()


def delete_address(db: Database, address_id: int) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM contact_addresses WHERE id = ?", (address_id,))
        conn.commit()


def set_default_address(db: Database, address_id: int, which: str) -> None:
    """Make this address the default for 'delivery' or 'invoice' (also marks it usable for that)."""
    col = "is_default_delivery" if which == "delivery" else "is_default_invoice"
    usage = "is_delivery" if which == "delivery" else "is_invoice"
    with db.connect() as conn:
        row = conn.execute(
            "SELECT contact_id FROM contact_addresses WHERE id = ?", (address_id,)).fetchone()
        if row is None:
            return
        conn.execute(f"UPDATE contact_addresses SET {col} = 0 WHERE contact_id = ?",
                     (row["contact_id"],))
        conn.execute(f"UPDATE contact_addresses SET {col} = 1, {usage} = 1, "
                     "updated_at = datetime('now') WHERE id = ?", (address_id,))
        conn.commit()


def _apply_default_flags(conn, contact_id: int, address_id: int, data: dict) -> None:
    """Keep at most one default delivery and one default invoice per contact. A row flagged default
    is implicitly usable for that purpose."""
    for default_col, usage_col in (("is_default_delivery", "is_delivery"),
                                   ("is_default_invoice", "is_invoice")):
        if data.get(default_col):
            conn.execute(f"UPDATE contact_addresses SET {default_col} = 0 WHERE contact_id = ?",
                         (contact_id,))
            conn.execute(f"UPDATE contact_addresses SET {default_col} = 1, {usage_col} = 1 "
                         "WHERE id = ?", (address_id,))
