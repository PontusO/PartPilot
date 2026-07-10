"""CRUD for the devmgmt device-catalog tables (models, variants, device builds).

All writes are idempotent upserts keyed by the shared ``ref`` / ``sku`` / ``serial`` — the same
keys devmgmt upserts on — so a re-run (e.g. after a failed push) never duplicates. JSON-shaped
columns are (de)serialized here so the rest of the app deals in plain lists/dicts.

owner_token is generated here (PartPilot is the issuer, docs §7) and stored in plaintext because
PartPilot needs it to (re)generate the device QR; devmgmt keeps only its hash.
"""

from __future__ import annotations

import json
import secrets

from ...core.db import Database
from . import devmgmt_outbox

# 32 bytes -> 64 hex chars. High-entropy per-device secret the customer's claim must present.
_OWNER_TOKEN_BYTES = 32


def generate_owner_token() -> str:
    """A fresh high-entropy owner token (docs §7)."""
    return secrets.token_hex(_OWNER_TOKEN_BYTES)


# -- models + board revisions ----------------------------------------------

def upsert_model(db: Database, *, ref: str, name: str,
                 radio_capabilities: list[str],
                 board_revisions: list[dict]) -> int:
    """Upsert a product model and (re)sync its board revisions. Returns the model id.

    ``board_revisions`` is a list of ``{"ref": ..., "rev": ...}``. Existing rows for the model are
    replaced wholesale so an edit that drops a revision is honoured."""
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO product_models (ref, name, radio_capabilities)
            VALUES (?, ?, ?)
            ON CONFLICT(ref) DO UPDATE SET
                name = excluded.name,
                radio_capabilities = excluded.radio_capabilities,
                updated_at = datetime('now')
            """,
            (ref, name, json.dumps(radio_capabilities or [])),
        )
        model_id = conn.execute(
            "SELECT id FROM product_models WHERE ref = ?", (ref,)
        ).fetchone()[0]
        # Removing a revision that device build records still reference (soft link, no FK) would
        # make those devices' payloads referentially invalid — devmgmt would 409 every re-push.
        keep = {br["rev"] for br in board_revisions or []}
        placeholders = ",".join("?" for _ in keep)
        in_use = [r[0] for r in conn.execute(
            "SELECT DISTINCT d.board_rev FROM device_builds d "
            "JOIN variants v ON v.id = d.variant_id WHERE v.model_id = ?"
            + (f" AND d.board_rev NOT IN ({placeholders})" if keep else ""),
            (model_id, *keep),
        )]
        if in_use:
            raise ValueError(
                f"Can't remove board revision(s) {', '.join(sorted(in_use))} — "
                "device build records still reference them.")
        conn.execute("DELETE FROM board_revisions WHERE model_id = ?", (model_id,))
        for br in board_revisions or []:
            conn.execute(
                "INSERT INTO board_revisions (model_id, ref, rev) VALUES (?, ?, ?)",
                (model_id, br["ref"], br["rev"]),
            )
        # Catalog-edit trigger: queue a push in the same transaction as the edit.
        devmgmt_outbox.enqueue(conn, "model", ref)
        conn.commit()
        return model_id


def get_model(db: Database, ref: str) -> dict | None:
    """A model + its board revisions, JSON columns parsed. None if not found."""
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM product_models WHERE ref = ?", (ref,)).fetchone()
        if not row:
            return None
        model = _model_to_dict(row)
        model["board_revisions"] = [
            {"ref": r["ref"], "rev": r["rev"]}
            for r in conn.execute(
                "SELECT ref, rev FROM board_revisions WHERE model_id = ? ORDER BY id",
                (row["id"],),
            )
        ]
        return model


# -- variants + flashable targets ------------------------------------------

def upsert_variant(db: Database, *, ref: str, model_ref: str, sku: str,
                   enabled_radios: list[str],
                   radio_config: dict | None = None,
                   assembly_id: int | None = None,
                   flashable_targets: list[dict]) -> int:
    """Upsert a variant/SKU and (re)sync its flashable targets. Returns the variant id.

    Raises ValueError if ``model_ref`` is unknown — PartPilot's own referential guard, mirroring
    the 409 devmgmt would return for a variant that arrives before its model."""
    with db.connect() as conn:
        model = conn.execute(
            "SELECT id FROM product_models WHERE ref = ?", (model_ref,)
        ).fetchone()
        if not model:
            raise ValueError(f"Unknown model_ref {model_ref!r}; upsert the model first.")
        conn.execute(
            """
            INSERT INTO variants (ref, model_id, assembly_id, sku, enabled_radios, radio_config)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(ref) DO UPDATE SET
                model_id = excluded.model_id,
                assembly_id = excluded.assembly_id,
                sku = excluded.sku,
                enabled_radios = excluded.enabled_radios,
                radio_config = excluded.radio_config,
                updated_at = datetime('now')
            """,
            (ref, model["id"], assembly_id, sku,
             json.dumps(enabled_radios or []),
             json.dumps(radio_config) if radio_config is not None else None),
        )
        variant_id = conn.execute(
            "SELECT id FROM variants WHERE ref = ?", (ref,)
        ).fetchone()[0]
        conn.execute("DELETE FROM variant_flashable_targets WHERE variant_id = ?", (variant_id,))
        for i, t in enumerate(flashable_targets or []):
            conn.execute(
                """INSERT INTO variant_flashable_targets
                       (variant_id, component, factory_firmware_ref, update_method, line_no)
                       VALUES (?, ?, ?, ?, ?)""",
                (variant_id, t["component"], t["factory_firmware_ref"], t["update_method"], i),
            )
        # Catalog-edit trigger: queue a push in the same transaction as the edit.
        devmgmt_outbox.enqueue(conn, "variant", ref)
        conn.commit()
        return variant_id


def get_variant(db: Database, ref: str) -> dict | None:
    """A variant + its flashable targets and its model's ref, JSON columns parsed. None if absent."""
    with db.connect() as conn:
        row = conn.execute(
            """SELECT v.*, m.ref AS model_ref
                 FROM variants v JOIN product_models m ON m.id = v.model_id
                WHERE v.ref = ?""",
            (ref,),
        ).fetchone()
        if not row:
            return None
        variant = _variant_to_dict(row)
        variant["flashable_targets"] = [
            {"component": t["component"],
             "factory_firmware_ref": t["factory_firmware_ref"],
             "update_method": t["update_method"]}
            for t in conn.execute(
                """SELECT component, factory_firmware_ref, update_method
                     FROM variant_flashable_targets WHERE variant_id = ? ORDER BY line_no, id""",
                (row["id"],),
            )
        ]
        return variant


# -- device build records --------------------------------------------------

def create_device(db: Database, *, serial: str, variant_ref: str, board_rev: str,
                  radios: list[dict], work_order_id: int | None = None,
                  owner_token: str | None = None) -> str:
    """Record a provisioned device (docs §5.3), generating an owner_token if not supplied.

    Upserts on ``serial`` so re-running the tester intake for the same unit is safe. Returns the
    device's owner_token (the caller needs it for the QR). Raises ValueError if the variant is
    unknown or the board_rev isn't one the model declares."""
    with db.connect() as conn:
        variant = conn.execute(
            "SELECT id, model_id FROM variants WHERE ref = ?", (variant_ref,)
        ).fetchone()
        if not variant:
            raise ValueError(f"Unknown variant_ref {variant_ref!r}; upsert the variant first.")
        known_rev = conn.execute(
            "SELECT 1 FROM board_revisions WHERE model_id = ? AND rev = ?",
            (variant["model_id"], board_rev),
        ).fetchone()
        if not known_rev:
            raise ValueError(
                f"board_rev {board_rev!r} isn't a revision of variant {variant_ref!r}'s model.")
        token = owner_token or generate_owner_token()
        already_pushed = conn.execute(
            "SELECT 1 FROM device_builds WHERE serial = ? AND pushed_at IS NOT NULL", (serial,)
        ).fetchone() is not None
        conn.execute(
            """
            INSERT INTO device_builds (serial, variant_id, board_rev, owner_token, radios,
                                       work_order_id)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(serial) DO UPDATE SET
                variant_id = excluded.variant_id,
                board_rev = excluded.board_rev,
                radios = excluded.radios,
                work_order_id = excluded.work_order_id,
                updated_at = datetime('now')
            """,
            (serial, variant["id"], board_rev, token, json.dumps(radios or []), work_order_id),
        )
        if already_pushed:
            # Re-provisioning an already-pushed device (e.g. corrected radio identities): devmgmt
            # now holds stale data, so mark it un-pushed and queue an immediate re-push — the
            # WO-finish trigger only picks up devices with pushed_at IS NULL and the WO that built
            # this unit has usually already finished.
            conn.execute(
                "UPDATE device_builds SET pushed_at = NULL WHERE serial = ?", (serial,))
            devmgmt_outbox.enqueue(conn, "device", serial)
        conn.commit()
        # On an update we keep the original owner_token (the QR is already out there); read it back.
        return conn.execute(
            "SELECT owner_token FROM device_builds WHERE serial = ?", (serial,)
        ).fetchone()[0]


def set_variant_retired(db: Database, ref: str, retired: bool) -> bool:
    """Retire (or un-retire) a variant: set/clear ``retired_at`` and re-queue a push so devmgmt
    gets the new ``retired`` flag (docs §7 option 1). Returns True if the variant existed."""
    with db.connect() as conn:
        cur = conn.execute(
            "UPDATE variants SET retired_at = "
            + ("datetime('now')" if retired else "NULL")
            + ", updated_at = datetime('now') WHERE ref = ?",
            (ref,),
        )
        if cur.rowcount:
            devmgmt_outbox.enqueue(conn, "variant", ref)
        conn.commit()
        return cur.rowcount > 0


def set_model_retired(db: Database, ref: str, retired: bool) -> bool:
    """Retire / un-retire a model (docs §7). Returns True if the model existed."""
    with db.connect() as conn:
        cur = conn.execute(
            "UPDATE product_models SET retired_at = "
            + ("datetime('now')" if retired else "NULL")
            + ", updated_at = datetime('now') WHERE ref = ?",
            (ref,),
        )
        if cur.rowcount:
            devmgmt_outbox.enqueue(conn, "model", ref)
        conn.commit()
        return cur.rowcount > 0


def variant_device_count(db: Database, ref: str) -> int:
    """How many device build records reference this variant — the delete guard (docs §7)."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM device_builds d JOIN variants v ON v.id = d.variant_id "
            "WHERE v.ref = ?",
            (ref,),
        ).fetchone()
        return int(row[0])


def delete_variant(db: Database, ref: str) -> None:
    """Hard-delete a variant locally and queue the devmgmt DELETE (docs §7). Callers MUST have
    checked the guards first (retired + no referencing devices). Done in one transaction: drop any
    pending upsert for this ref (superseded), enqueue the delete job, then remove the row."""
    with db.connect() as conn:
        conn.execute("DELETE FROM devmgmt_outbox WHERE kind = 'variant' AND ref = ?", (ref,))
        devmgmt_outbox.enqueue(conn, "delete-variant", ref)
        conn.execute("DELETE FROM variants WHERE ref = ?", (ref,))
        conn.commit()


def product_for_assembly(db: Database, assembly_id: int) -> dict | None:
    """The devmgmt product (variant + its model) linked to this assembly, or None if unpublished.

    An assembly maps to at most one variant through the publish UI; if several exist (created via
    the CLI) the lowest-id one is returned. Shape: ``{"variant": {...}, "model": {...}}`` with the
    same parsed dicts ``get_variant``/``get_model`` return."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT ref FROM variants WHERE assembly_id = ? ORDER BY id LIMIT 1", (assembly_id,)
        ).fetchone()
    if row is None:
        return None
    variant = get_variant(db, row["ref"])
    model = get_model(db, variant["model_ref"]) if variant else None
    return {"variant": variant, "model": model}


def get_device(db: Database, serial: str) -> dict | None:
    """A device build record + its variant_ref, ``radios`` parsed. None if not found."""
    with db.connect() as conn:
        row = conn.execute(
            """SELECT d.*, v.ref AS variant_ref
                 FROM device_builds d JOIN variants v ON v.id = d.variant_id
                WHERE d.serial = ?""",
            (serial,),
        ).fetchone()
        return _device_to_dict(row) if row else None


def mark_device_pushed(db: Database, serial: str) -> None:
    """Stamp the device as successfully pushed to devmgmt (docs §5.3 completed)."""
    with db.connect() as conn:
        conn.execute(
            "UPDATE device_builds SET pushed_at = datetime('now') WHERE serial = ?", (serial,)
        )
        conn.commit()


# -- row -> dict helpers ----------------------------------------------------

def _model_to_dict(row) -> dict:
    d = dict(row)
    d["radio_capabilities"] = json.loads(d.get("radio_capabilities") or "[]")
    return d


def _variant_to_dict(row) -> dict:
    d = dict(row)
    d["enabled_radios"] = json.loads(d.get("enabled_radios") or "[]")
    d["radio_config"] = json.loads(d["radio_config"]) if d.get("radio_config") else None
    return d


def _device_to_dict(row) -> dict:
    d = dict(row)
    d["radios"] = json.loads(d.get("radios") or "[]")
    return d
