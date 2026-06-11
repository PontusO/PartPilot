"""Key/value app settings, with a typed view of the company profile used on documents."""

from __future__ import annotations

from ...core.db import Database

# Company profile fields (stored as app_settings keys "company.<field>").
COMPANY_FIELDS = ("name", "address", "postcode", "city", "country",
                  "vat_no", "org_no", "email", "phone", "website")


def get_setting(db: Database, key: str) -> str | None:
    with db.connect() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(db: Database, key: str, value: str | None) -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()


def get_company(db: Database) -> dict:
    with db.connect() as conn:
        rows = dict(conn.execute(
            "SELECT key, value FROM app_settings WHERE key LIKE 'company.%'").fetchall())
    return {f: rows.get(f"company.{f}", "") or "" for f in COMPANY_FIELDS}


def save_company(db: Database, data: dict) -> None:
    with db.connect() as conn:
        for f in COMPANY_FIELDS:
            conn.execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (f"company.{f}", (data.get(f) or "").strip() or None),
            )
        conn.commit()


# ---- production settings ----

def get_production(db: Database) -> dict:
    return {"spillage_percent": get_setting(db, "production.spillage_percent") or "",
            "min_margin_qty": get_setting(db, "production.min_margin_qty") or ""}


def save_production(db: Database, data: dict) -> None:
    set_setting(db, "production.spillage_percent", (data.get("spillage_percent") or "").strip() or None)
    set_setting(db, "production.min_margin_qty", (data.get("min_margin_qty") or "").strip() or None)


# ---- order settings ----

def get_orders(db: Database) -> dict:
    # Default ON (unset → True) so existing behaviour is unchanged; only an explicit "0" turns it off.
    return {"ack_confirms": get_setting(db, "orders.ack_confirms") != "0"}


def save_orders(db: Database, data: dict) -> None:
    set_setting(db, "orders.ack_confirms", "1" if data.get("ack_confirms") else "0")
