"""Part/assembly versioning write flows.

A *version* is a `parts` row: multiple versions of one item share a `part_no` but are distinct
`parts.id`s tied together by `version_group_id`, each with its own `bom_lines`, stock and suppliers.
Because every downstream reference (bom_lines, work_orders, orders, stock) points at `parts.id`, a
record raised against one version stays pinned to it — history stays truthful with no schema churn.

Lifecycle (`parts.version_status`), mirroring the Documents feature's current-flag pattern:
  draft      — editable, not orderable/buildable; the version being prepared.
  released   — the single current default per group (enforced by the `ux_parts_released` partial
               unique index); what "resolve by number" returns.
  superseded — a past release, still selectable for new work only when explicitly chosen.

Kept separate from ``repo.py`` (read queries + the add-a-component writes) so the versioning machinery
is easy to find. All SQL is raw sqlite3, same as the rest of the feature.
"""

from __future__ import annotations

from ...core.db import Database

# Columns whose value must NOT be copied from the source version when cloning. `id` is a fresh PK;
# created_at/updated_at fall back to their `datetime('now')` DEFAULT (so the clone is stamped now).
_PARTS_SKIP = {"id", "created_at", "updated_at"}


def _columns(conn, table: str) -> list[str]:
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]


def _clone_row(conn, table: str, src, *, skip: set[str], overrides: dict) -> int:
    """Insert a copy of ``src`` (a sqlite3.Row) into ``table``, dropping ``skip`` columns (they take
    their table default) and replacing ``overrides``. Introspects columns so new ALTERs are copied
    automatically."""
    cols = [c for c in _columns(conn, table) if c not in skip]
    vals = [overrides[c] if c in overrides else src[c] for c in cols]
    placeholders = ", ".join("?" for _ in cols)
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
    return conn.execute(sql, vals).lastrowid


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def group_id_for(db: Database, part_id: int) -> int | None:
    """The version group a part belongs to (falls back to its own id for a legacy un-grouped row)."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT version_group_id, id FROM parts WHERE id = ?", (part_id,)
        ).fetchone()
    if row is None:
        return None
    return row["version_group_id"] or row["id"]


def create_version(db: Database, source_part_id: int, *, rev: str, user: str | None = None) -> int:
    """Clone ``source_part_id`` into a new *draft* version in the same group and return its id.

    Copies the parts row (new id, ``minimrp_id`` NULL, stock counters 0, fresh timestamps,
    ``version_status='draft'``, same ``part_no``/``kind``/``version_group_id``, ``rev`` replaced), its
    ``bom_lines`` (so an assembly's whole BOM comes along), its supplier offers + their captured cost
    tiers, and its customer sell tiers. Stock is deliberately NOT copied — a new version starts empty.
    """
    with db.connect() as conn:
        src = conn.execute("SELECT * FROM parts WHERE id = ?", (source_part_id,)).fetchone()
        if src is None:
            raise ValueError(f"part {source_part_id} not found")
        group = src["version_group_id"] or src["id"]

        overrides = {
            "minimrp_id": None,
            "total_qty": 0,
            "total_alloc": 0,
            "total_on_order": 0,
            "version_status": "draft",
            "version_group_id": group,
            "rev": (rev or "").strip() or None,
        }
        # zero any other stock-ish counters that later ALTERs may have added
        if "webshop_synced_qty" in _columns(conn, "parts"):
            overrides["webshop_synced_qty"] = None
        new_id = _clone_row(conn, "parts", src, skip=_PARTS_SKIP, overrides=overrides)

        # BOM lines (assemblies feature owns the table; guard so catalog-only contexts don't break).
        if _table_exists(conn, "bom_lines"):
            for ln in conn.execute(
                "SELECT * FROM bom_lines WHERE parent_id = ?", (source_part_id,)
            ).fetchall():
                _clone_row(conn, "bom_lines", ln, skip={"id"},
                           overrides={"parent_id": new_id, "minimrp_id": None})

        # Supplier offers + their captured cost tiers (tiers hang off the offer id, so remap).
        for ps in conn.execute(
            "SELECT * FROM part_suppliers WHERE part_id = ?", (source_part_id,)
        ).fetchall():
            new_ps_id = _clone_row(conn, "part_suppliers", ps, skip={"id"},
                                   overrides={"part_id": new_id, "minimrp_id": None})
            for tier in conn.execute(
                "SELECT * FROM part_supplier_tiers WHERE part_supplier_id = ?", (ps["id"],)
            ).fetchall():
                _clone_row(conn, "part_supplier_tiers", tier, skip={"id"},
                           overrides={"part_supplier_id": new_ps_id})

        # Customer sell tiers.
        for t in conn.execute(
            "SELECT * FROM part_price_tiers WHERE part_id = ?", (source_part_id,)
        ).fetchall():
            _clone_row(conn, "part_price_tiers", t, skip={"id"}, overrides={"part_id": new_id})

        conn.commit()
    return new_id


def release_version(db: Database, part_id: int) -> None:
    """Make ``part_id`` the current released version of its group; demote the previous release to
    superseded. Works from draft or superseded; releasing an already-released version is a no-op.
    Demote-then-promote keeps the ``ux_parts_released`` partial unique index satisfied throughout."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT version_group_id, id, version_status FROM parts WHERE id = ?", (part_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"part {part_id} not found")
        if row["version_status"] == "released":
            return
        group = row["version_group_id"] or row["id"]
        conn.execute(
            "UPDATE parts SET version_status='superseded', updated_at=datetime('now') "
            "WHERE version_group_id = ? AND version_status='released' AND id != ?",
            (group, part_id),
        )
        conn.execute(
            "UPDATE parts SET version_status='released', updated_at=datetime('now') WHERE id = ?",
            (part_id,),
        )
        conn.commit()


def list_versions(db: Database, group_id: int) -> list[dict]:
    """All versions in a group, current (released) first, then by id. Drives the Versions panel."""
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, part_no, rev, kind, version_status, total_qty, total_alloc, updated_at "
            "FROM parts WHERE version_group_id = ? "
            "ORDER BY (version_status='released') DESC, id",
            (group_id,),
        )]


def current_version_id(db: Database, group_id: int) -> int | None:
    """The released version's id for a group, or None if the group has only a draft."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM parts WHERE version_group_id = ? AND version_status='released' LIMIT 1",
            (group_id,),
        ).fetchone()
    return row["id"] if row else None


def current_version_for_part_no(db: Database, part_no: str | None) -> dict | None:
    """Resolve a human part number to its current version: prefer released, else the lowest-id row
    (a group that's only been drafted). Callers treat a non-released result as not orderable."""
    if not (part_no or "").strip():
        return None
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM parts WHERE lower(part_no) = lower(?) "
            "ORDER BY (version_status='released') DESC, id LIMIT 1",
            (part_no.strip(),),
        ).fetchone()
    return dict(row) if row else None
