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


# ---- webshop (WooCommerce) settings ----

# Connection details for the WooCommerce sync, stored as "webshop.<field>". ``currency`` is an
# optional override appended to read requests (only needed on multi-currency shops).
WEBSHOP_FIELDS = ("base_url", "consumer_key", "consumer_secret", "currency")


def get_webshop(db: Database) -> dict:
    with db.connect() as conn:
        rows = dict(conn.execute(
            "SELECT key, value FROM app_settings WHERE key LIKE 'webshop.%'").fetchall())
    data = {f: rows.get(f"webshop.{f}", "") or "" for f in WEBSHOP_FIELDS}
    data["last_sync_at"] = rows.get("webshop.last_sync_at") or ""
    data["configured"] = bool(data["base_url"] and data["consumer_key"] and data["consumer_secret"])
    return data


def save_webshop(db: Database, data: dict) -> None:
    for f in WEBSHOP_FIELDS:
        set_setting(db, f"webshop.{f}", (data.get(f) or "").strip() or None)


def set_webshop_synced(db: Database, when: str) -> None:
    set_setting(db, "webshop.last_sync_at", when)


# ---- Fortnox (accounting) settings + OAuth tokens ----

# Integration config, stored as "fortnox.<field>". Tokens live under fortnox.access_token /
# fortnox.refresh_token / fortnox.token_expires_at and are managed separately (they rotate).
FORTNOX_FIELDS = ("client_id", "client_secret", "redirect_uri", "default_vat", "default_account")


def get_fortnox(db: Database) -> dict:
    with db.connect() as conn:
        rows = dict(conn.execute(
            "SELECT key, value FROM app_settings WHERE key LIKE 'fortnox.%'").fetchall())
    data = {f: rows.get(f"fortnox.{f}", "") or "" for f in FORTNOX_FIELDS}
    data["default_vat"] = data["default_vat"] or "25"        # Swedish standard rate
    data["configured"] = bool(data["client_id"] and data["client_secret"] and data["redirect_uri"])
    data["connected"] = bool(rows.get("fortnox.refresh_token"))
    return data


def save_fortnox(db: Database, data: dict) -> None:
    for f in FORTNOX_FIELDS:
        set_setting(db, f"fortnox.{f}", (data.get(f) or "").strip() or None)


def save_fortnox_tokens(db: Database, tokens) -> None:
    set_setting(db, "fortnox.access_token", tokens.access_token)
    set_setting(db, "fortnox.refresh_token", tokens.refresh_token)
    set_setting(db, "fortnox.token_expires_at", tokens.expires_at.isoformat())


def load_fortnox_tokens(db: Database):
    """Return the stored FortnoxTokens, or None if the integration isn't connected."""
    from datetime import datetime

    from digisearch.fortnox import FortnoxTokens

    access = get_setting(db, "fortnox.access_token")
    refresh = get_setting(db, "fortnox.refresh_token")
    expires = get_setting(db, "fortnox.token_expires_at")
    if not (access and refresh and expires):
        return None
    return FortnoxTokens(access, refresh, datetime.fromisoformat(expires))


def clear_fortnox_tokens(db: Database) -> None:
    for k in ("access_token", "refresh_token", "token_expires_at"):
        set_setting(db, f"fortnox.{k}", None)
