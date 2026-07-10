"""The devmgmt push outbox — enqueue side (in-transaction) and flush side (background).

The two auto-triggers write to this table in the SAME transaction as the change they react to
(``enqueue`` takes a live sqlite connection), so "we owe devmgmt a push" is as durable as the edit
itself and no network call ever sits in the request path. The background loop (``devmgmt_sync``)
calls ``flush`` to drain pending jobs to devmgmt with retry.

Referential ordering: jobs are drained model → variant → device so a variant never lands before its
model. Each job is also self-sufficient — a variant job re-upserts its model first, a device job
pushes the whole model→variant→device chain — so a lone device job still can't 409.
"""

from __future__ import annotations

from ....devmgmt import DevmgmtConflictError, DevmgmtError, DevmgmtPayloadError
from ...core.db import Database

# A retryable job (network / 5xx / referential 409) is left pending and retried with exponential
# backoff, forever — the outbox exists to survive devmgmt outages, so transient failures never
# flip to 'error'. Only terminal failures (bad payload, refused delete, vanished source row) do.
BACKOFF_BASE_SECONDS = 20    # first retry delay; doubles per attempt...
BACKOFF_MAX_SECONDS = 3600   # ...capped so a long outage is still probed at least hourly.

# Deletes sort last, but a pending delete and a pending upsert for the same ref can never coexist:
# enqueue() drops a queued delete when the object is re-created, and delete_variant() drops the
# queued upsert — so this ordering can't replay a delete on top of a newer re-create.
_KIND_ORDER = "CASE kind WHEN 'model' THEN 0 WHEN 'variant' THEN 1 WHEN 'device' THEN 2 ELSE 3 END"

_DUE = "(next_attempt_at IS NULL OR next_attempt_at <= datetime('now'))"


# -- enqueue side (called inside another feature's transaction) -------------

def enqueue(conn, kind: str, ref: str) -> None:
    """Record that ``ref`` (a model/variant/device) needs pushing. Idempotent per (kind, ref):
    re-enqueuing an object resets it to pending with a fresh attempt count (and bumps ``seq`` so an
    in-flight flush can't mark the fresh job done) so a later edit is always re-sent. Re-creating
    an object also supersedes any still-queued hard delete of the same ref — the upsert overwrites
    the remote object in place, so replaying the old delete afterwards would be wrong. Operates on
    the caller's connection — no commit here (the caller owns it)."""
    conn.execute(
        "DELETE FROM devmgmt_outbox WHERE kind = ? AND ref = ? AND status = 'pending'",
        (f"delete-{kind}", ref),
    )
    conn.execute(
        """
        INSERT INTO devmgmt_outbox (kind, ref, status, attempts, last_error)
        VALUES (?, ?, 'pending', 0, NULL)
        ON CONFLICT(kind, ref) DO UPDATE SET
            status = 'pending', attempts = 0, last_error = NULL, next_attempt_at = NULL,
            seq = seq + 1, updated_at = datetime('now')
        """,
        (kind, ref),
    )


def enqueue_for_finished_work_order(conn, wo_id: int) -> None:
    """WO-finish trigger: enqueue the finished assembly's variant(s) and any devices built on this
    WO that aren't pushed yet. A normal (non-device) assembly maps to no variant → enqueues nothing.
    The variant is enqueued too so devmgmt has the catalog projection before the devices arrive."""
    wo = conn.execute("SELECT assembly_id FROM work_orders WHERE id = ?", (wo_id,)).fetchone()
    if wo is None:
        return
    for variant in conn.execute(
        "SELECT ref FROM variants WHERE assembly_id = ?", (wo["assembly_id"],)
    ):
        enqueue(conn, "variant", variant["ref"])
    for device in conn.execute(
        "SELECT serial FROM device_builds WHERE work_order_id = ? AND pushed_at IS NULL", (wo_id,)
    ):
        enqueue(conn, "device", device["serial"])


# -- reads -----------------------------------------------------------------

def has_pending(db: Database) -> bool:
    """Cheap check the background loop uses to avoid building a client when there's nothing to do.
    Jobs backed off into the future don't count — they aren't actionable this tick."""
    with db.connect() as conn:
        row = conn.execute(
            f"SELECT EXISTS(SELECT 1 FROM devmgmt_outbox WHERE status = 'pending' AND {_DUE})"
        ).fetchone()
        return bool(row[0])


def status_for(db: Database, kind: str, ref: str) -> dict | None:
    """The outbox row for one object (its sync state), or None if it was never enqueued."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT status, attempts, last_error, updated_at FROM devmgmt_outbox "
            "WHERE kind = ? AND ref = ?",
            (kind, ref),
        ).fetchone()
        return dict(row) if row else None


def enqueue_product(db: Database, model_ref: str, variant_ref: str) -> None:
    """Re-queue a product's model + variant for pushing (the panel's manual 'Push now'). Opens its
    own transaction, unlike the in-transaction ``enqueue`` the auto-triggers use."""
    with db.connect() as conn:
        enqueue(conn, "model", model_ref)
        enqueue(conn, "variant", variant_ref)
        conn.commit()


def pending_jobs(db: Database) -> list[dict]:
    """Due pending jobs in referential order (models first, then variants, then devices). Jobs
    whose retry backoff hasn't elapsed are skipped until they're due."""
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT * FROM devmgmt_outbox WHERE status = 'pending' AND {_DUE} "
            f"ORDER BY {_KIND_ORDER}, id"
        )]


# -- flush side (called by the background loop) ----------------------------

def flush(db: Database, client) -> dict:
    """Drain all due pending jobs to devmgmt. Returns a counts dict {pushed, retry, errored}.

    Never raises: a bad payload / missing data marks the job 'error' (won't retry); a transient
    failure (network, 5xx, 409) leaves it pending, backed off exponentially, and is retried
    indefinitely — the outbox's durability promise is that a devmgmt outage of any length only
    delays pushes, never loses them."""
    # Imported here (not at module top) to avoid an import cycle: devmgmt_repo imports this module
    # for the enqueue calls, and devmgmt_push imports devmgmt_repo.
    from . import devmgmt_push, devmgmt_repo

    report = {"pushed": 0, "retry": 0, "errored": 0}
    for job in pending_jobs(db):
        try:
            _push_job(db, client, job, devmgmt_push, devmgmt_repo)
        except (DevmgmtPayloadError, DevmgmtConflictError) as exc:  # bad payload / blocked delete —
            _set_status(db, job, "error", str(exc))                # retrying won't help
            report["errored"] += 1
        except ValueError as exc:                    # the referenced object vanished / bad data
            _set_status(db, job, "error", str(exc))
            report["errored"] += 1
        except DevmgmtError as exc:                  # network / 5xx / 409 — retry with backoff
            _record_attempt(db, job, str(exc))
            report["retry"] += 1
        else:
            _set_status(db, job, "done", None)
            report["pushed"] += 1
    return report


def _push_job(db: Database, client, job: dict, devmgmt_push, devmgmt_repo) -> None:
    """Push one outbox job. Each kind pushes its own dependencies first so it can't 409 on its own."""
    kind, ref = job["kind"], job["ref"]
    if kind == "model":
        model = devmgmt_repo.get_model(db, ref)
        if model is None:
            raise ValueError(f"model {ref!r} no longer exists")
        client.upsert_model(devmgmt_push.model_payload(model))
    elif kind == "variant":
        variant = devmgmt_repo.get_variant(db, ref)
        if variant is None:
            raise ValueError(f"variant {ref!r} no longer exists")
        model = devmgmt_repo.get_model(db, variant["model_ref"])
        if model is None:
            raise ValueError(f"variant {ref!r} references missing model {variant['model_ref']!r}")
        client.upsert_model(devmgmt_push.model_payload(model))       # dependency first
        client.upsert_variant(devmgmt_push.variant_payload(variant))
    elif kind == "device":
        # push_device sends model → variant → device in order and stamps pushed_at on success.
        devmgmt_push.push_device(db, client, ref)
    elif kind == "delete-variant":
        # The local row is already gone; just propagate the hard delete (idempotent — 404 is ok).
        client.delete_variant(ref)
    elif kind == "delete-model":
        client.delete_model(ref)
    else:
        raise ValueError(f"unknown outbox kind {kind!r}")


def _set_status(db: Database, job: dict, status: str, error: str | None) -> None:
    """Record the outcome of the job *as snapshotted* — the ``seq`` guard makes this a no-op if a
    request thread re-enqueued the same (kind, ref) while the push was in flight: the push used
    pre-edit data, so the fresh pending job must survive to re-send the edit next tick."""
    with db.connect() as conn:
        conn.execute(
            "UPDATE devmgmt_outbox SET status = ?, last_error = ?, updated_at = datetime('now') "
            "WHERE id = ? AND seq = ?",
            (status, error, job["id"], job["seq"]),
        )
        conn.commit()


def _record_attempt(db: Database, job: dict, error: str) -> None:
    """Count a failed attempt and back the job off exponentially (same ``seq`` guard as above)."""
    delay = min(BACKOFF_BASE_SECONDS * 2 ** job["attempts"], BACKOFF_MAX_SECONDS)
    with db.connect() as conn:
        conn.execute(
            "UPDATE devmgmt_outbox SET attempts = attempts + 1, last_error = ?, "
            "next_attempt_at = datetime('now', ?), updated_at = datetime('now') "
            "WHERE id = ? AND seq = ?",
            (error, f"+{delay} seconds", job["id"], job["seq"]),
        )
        conn.commit()
