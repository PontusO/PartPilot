"""Key/value app settings, with a typed view of the company profile used on documents."""

from __future__ import annotations

import sqlite3

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


# ---- part-number cleanup tool ----

def find_suspect_parts(db: Database) -> list[dict]:
    """Parts whose ``part_no`` is really a *supplier* order code (it matches one of the part's
    own supplier rows) and which have no manufacturer P/N — i.e. the manufacturer identity was
    never captured. Each row carries its supplier list so the tool can re-query the right
    distributor."""
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT DISTINCT p.id, p.part_no, p.value, p.category, p.mfr_name
               FROM parts p
               JOIN part_suppliers ps ON ps.part_id = p.id
               WHERE (p.mfr_pno IS NULL OR p.mfr_pno = '')
                 AND ps.supplier_pno IS NOT NULL
                 AND replace(lower(p.part_no), ' ', '') = replace(lower(ps.supplier_pno), ' ', '')
               ORDER BY p.part_no""").fetchall()
        out = []
        for r in rows:
            sups = conn.execute(
                """SELECT s.name AS supplier, ps.supplier_pno
                   FROM part_suppliers ps LEFT JOIN suppliers s ON s.id = ps.supplier_id
                   WHERE ps.part_id = ? AND ps.supplier_pno IS NOT NULL""", (r["id"],)).fetchall()
            d = dict(r)
            d["suppliers"] = [dict(s) for s in sups]
            out.append(d)
    return out


def part_no_taken(db: Database, part_id: int, mpn: str) -> int | None:
    """If another part already uses ``mpn`` as its part_no or mfr_pno, return its id (a collision)."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM parts WHERE id != ? AND (part_no = ? OR mfr_pno = ?) LIMIT 1",
            (part_id, mpn, mpn)).fetchone()
    return row["id"] if row else None


def set_part_mpn(db: Database, part_id: int, mpn: str, manufacturer: str | None) -> None:
    """Promote a recovered manufacturer P/N: set both part_no and mfr_pno to it (we're naming a
    specific part), and fill the manufacturer when given. The supplier rows are left untouched —
    the old supplier order code already lives there."""
    with db.connect() as conn:
        conn.execute(
            "UPDATE parts SET part_no = ?, mfr_pno = ?, "
            "mfr_name = COALESCE(NULLIF(?, ''), mfr_name), updated_at = datetime('now') "
            "WHERE id = ?",
            (mpn, mpn, (manufacturer or "").strip(), part_id))
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


# ---- pricing settings ----

# Default sell markup applied to a part's cost when it has no explicit sell tiers and no per-part
# markup override. 1.30 = cost + 30 %. Stored as a bare float string under pricing.default_markup.
DEFAULT_MARKUP = 1.30


def get_default_markup(db: Database) -> float:
    """The configured default sell markup (> 0), or DEFAULT_MARKUP if unset/invalid. A markup of 0
    (or negative) is rejected — it would silently zero every generated sell price. Tolerates the
    setup feature (and its ``app_settings`` table) not being installed — pricing helpers in other
    features call this and must not hard-fail when Setup isn't part of a given app."""
    try:
        raw = get_setting(db, "pricing.default_markup")
    except sqlite3.OperationalError:
        return DEFAULT_MARKUP
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_MARKUP
    return value if value > 0 else DEFAULT_MARKUP


def get_pricing(db: Database) -> dict:
    return {"default_markup": get_default_markup(db)}


def save_pricing(db: Database, data: dict) -> None:
    raw = (str(data.get("default_markup") or "")).strip()
    try:
        value = float(raw)
    except ValueError:
        return  # ignore an unparseable submission, keep the current value
    if value > 0:      # 0 / negative would zero all sell prices — reject, keep the current value
        set_setting(db, "pricing.default_markup", repr(value))


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
    # Automatic pull-only sync: a daily run time "HH:MM" (blank/unset = off) + last-run bookkeeping.
    data["sync_at_time"] = rows.get("webshop.sync_at_time") or ""
    data["last_auto_sync_at"] = rows.get("webshop.last_auto_sync_at") or ""
    data["last_auto_sync_status"] = rows.get("webshop.last_auto_sync_status") or ""
    data["configured"] = bool(data["base_url"] and data["consumer_key"] and data["consumer_secret"])
    return data


def save_webshop(db: Database, data: dict) -> None:
    for f in WEBSHOP_FIELDS:
        set_setting(db, f"webshop.{f}", (data.get(f) or "").strip() or None)


def set_webshop_synced(db: Database, when: str) -> None:
    set_setting(db, "webshop.last_sync_at", when)


def set_webshop_time(db: Database, value) -> None:
    """Store the daily auto-sync time as normalized "HH:MM". Blank or unparseable disables it."""
    set_setting(db, "webshop.sync_at_time", normalize_hhmm(value))


def set_webshop_auto_status(db: Database, when: str, status: str) -> None:
    """Record the timestamp and outcome of the most recent automatic sync."""
    set_setting(db, "webshop.last_auto_sync_at", when)
    set_setting(db, "webshop.last_auto_sync_status", status)


def normalize_hhmm(value) -> str | None:
    """Return a canonical "HH:MM" (zero-padded, 24h) or None if blank/invalid."""
    from datetime import datetime

    try:
        return datetime.strptime(str(value).strip(), "%H:%M").strftime("%H:%M")
    except (TypeError, ValueError):
        return None


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
