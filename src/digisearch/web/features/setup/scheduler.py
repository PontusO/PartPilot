"""Background runner that pulls from the WooCommerce webshop once a day at a configured time.

The daily run time lives in the Setup & Tools control panel (``webshop.sync_at_time``, "HH:MM" in
``app_settings``); the loop re-reads it every tick, so changing it in the UI takes effect within one
poll without a restart. A fixed time (e.g. 05:00) keeps the pull out of working hours so stock never
shifts under people mid-day. Runs are **pull-only** (``push=False``): webshop sales and prices flow
into PartPilot, but the timer never writes stock back to the live shop — a deliberate, manual-only
action via the Sync button. The whole thing is gated off on scratch/test instances (see
``cli._make_scratch_db`` / ``PARTPILOT_DISABLE_SCHEDULER``) so a dev session can't sync the real shop.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from starlette.concurrency import run_in_threadpool

from digisearch.woocommerce import WooClient

from ...core.db import Database
from ..catalog import woo_sync
from . import repo

log = logging.getLogger("partpilot.webshop_scheduler")

POLL_SECONDS = 60     # how often we wake to check whether today's run is due / the time changed
GRACE_MINUTES = 60    # if the daemon was down at the scheduled time, still catch up within this long
                      # (but not later — we won't fire a "morning" sync in the middle of the workday)


def _is_due(sync_at: str | None, last_iso: str | None, now: datetime) -> bool:
    """True if a daily time is set, we're in the window after it today, and we haven't run since."""
    hhmm = repo.normalize_hhmm(sync_at) if sync_at else None
    if hhmm is None:
        return False
    hour, minute = (int(p) for p in hhmm.split(":"))
    scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < scheduled:
        return False                                            # not yet time today
    if (now - scheduled).total_seconds() > GRACE_MINUTES * 60:
        return False                                            # missed the window — wait for tomorrow
    if not last_iso:
        return True
    try:
        last = datetime.fromisoformat(last_iso)
    except ValueError:
        return True
    return last < scheduled                                     # haven't run since today's scheduled time


def _run_sync(database: Database) -> str:
    """Blocking pull-only sync against Woo. Returns a short human-readable status line."""
    settings = repo.get_webshop(database)
    if not settings["configured"]:
        return "skipped: webshop not configured"
    client = WooClient(settings["base_url"], settings["consumer_key"], settings["consumer_secret"],
                       currency=settings.get("currency") or None)
    report = woo_sync.sync_from_woo(database, list(client.iter_products()),
                                    client=client, user="auto-sync", dry_run=False, push=False)
    return (f"ok: {report.created} created, {report.updated} updated, "
            f"{report.pending_push} push pending, {len(report.errors)} error(s)")


async def webshop_sync_loop(database: Database, *, poll_seconds: int = POLL_SECONDS) -> None:
    """Run forever, syncing once a day at the configured time. Cancel to stop."""
    log.info("webshop auto-sync loop started (poll every %ss)", poll_seconds)
    try:
        while True:
            try:
                settings = repo.get_webshop(database)
                sync_at = settings["sync_at_time"]
                if (settings["configured"]
                        and _is_due(sync_at, settings["last_auto_sync_at"], datetime.now())):
                    log.info("webshop auto-sync due (scheduled %s) — running", sync_at)
                    try:
                        status = await run_in_threadpool(_run_sync, database)
                    except Exception as exc:  # network/Woo failure — record and retry tomorrow
                        status = f"error: {exc}"
                        log.exception("webshop auto-sync failed")
                    stamp = datetime.now().isoformat(timespec="seconds")
                    await run_in_threadpool(repo.set_webshop_auto_status, database, stamp, status)
                    log.info("webshop auto-sync done: %s", status)
            except Exception:  # never let a bad tick kill the loop
                log.exception("webshop scheduler tick error")
            await asyncio.sleep(poll_seconds)
    except asyncio.CancelledError:
        log.info("webshop auto-sync loop stopped")
        raise
