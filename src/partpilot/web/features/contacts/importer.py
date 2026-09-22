"""Bulk load of address-book rows into the contacts table (over already-parsed row dicts)."""

from __future__ import annotations

from ...core.db import Database

# (legacy source table, contact kind, source tag) — retained for reference / row-source tagging.
_SOURCES = [
    ("tblsupaddresses", "supplier", "sup"),
    ("tblcusaddresses", "customer", "cus"),
    ("tblmisaddresses", "other", "mis"),
]


def _s(x) -> str | None:
    s = (x or "").strip()
    return s or None


def _f(x) -> float | None:
    try:
        return float(x) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _i(x) -> int | None:
    try:
        return int(float(x)) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _address(row: dict) -> str | None:
    lines = [_s(row.get(f"Add{i}")) for i in range(1, 6)]
    joined = "\n".join(line for line in lines if line)
    return joined or None


def import_contact_rows(db: Database, *, kind: str, source: str, rows: list[dict]) -> int:
    """Upsert one source table's rows into contacts. Returns the row count."""
    with db.connect() as conn:
        for r in rows:
            conn.execute(
                """INSERT INTO contacts
                   (kind, name, short_name, contact, email, phone, phone2, fax, address,
                    postcode, website, currency, discount, notes, minimrp_id, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source, minimrp_id) DO UPDATE SET
                     kind=excluded.kind, name=excluded.name, short_name=excluded.short_name,
                     contact=excluded.contact, email=excluded.email, phone=excluded.phone,
                     phone2=excluded.phone2, fax=excluded.fax, address=excluded.address,
                     postcode=excluded.postcode, website=excluded.website,
                     currency=excluded.currency, discount=excluded.discount, notes=excluded.notes,
                     updated_at=datetime('now')""",
                (kind, _s(r.get("CoName")) or "?", _s(r.get("ShortNm")), _s(r.get("Contact1")),
                 _s(r.get("Email")) or _s(r.get("EMail")), _s(r.get("Tel1")), _s(r.get("Tel2")),
                 _s(r.get("Fax1")), _address(r), _s(r.get("PCode")), _s(r.get("URL")),
                 _s(r.get("defCurrency")), _f(r.get("Discount")), _s(r.get("Comment")),
                 _i(r.get("AddID")), source),
            )
        conn.commit()
    return len(rows)


