"""Queries + writes for the contacts address book."""

from __future__ import annotations

from ...core.db import Database

KINDS = ("supplier", "customer", "other")

_FIELDS = ("kind", "name", "short_name", "contact", "email", "phone", "phone2", "fax",
           "address", "postcode", "website", "currency", "discount", "notes")


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
