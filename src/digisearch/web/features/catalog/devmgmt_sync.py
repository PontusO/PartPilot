"""Background loop that drains the devmgmt push outbox to devmgmt.

Catalog edits and work-order completion enqueue jobs into ``devmgmt_outbox`` (in-transaction, no
network). This loop is the other half of the auto-triggers: it wakes periodically and, when devmgmt
is configured and there are pending jobs, pushes them with the client's own retry/backoff plus the
outbox's per-job attempt tracking. Modelled on the webshop scheduler; gated the same way
(``PARTPILOT_DISABLE_SCHEDULER``) so scratch/test instances never talk to devmgmt.
"""

from __future__ import annotations

import asyncio
import logging

from starlette.concurrency import run_in_threadpool

from digisearch.devmgmt import DevmgmtClient, DevmgmtConfig

from ...core.db import Database
from . import devmgmt_outbox

log = logging.getLogger("partpilot.devmgmt_sync")

POLL_SECONDS = 20  # how often to check the outbox for pending pushes


def _flush_once(database: Database) -> str:
    """One flush pass (blocking). Returns a short status line; never raises for expected states."""
    config = DevmgmtConfig.from_env()
    if config is None:
        return "skipped: devmgmt not configured"
    if not devmgmt_outbox.has_pending(database):
        return "idle"
    client = DevmgmtClient(config.base_url, auth=config.build_auth())
    report = devmgmt_outbox.flush(database, client)
    return (f"pushed {report['pushed']}, retry {report['retry']}, error {report['errored']}")


async def devmgmt_sync_loop(database: Database, *, poll_seconds: int = POLL_SECONDS) -> None:
    """Run forever, flushing the outbox whenever there's something to push. Cancel to stop."""
    log.info("devmgmt outbox sync loop started (poll every %ss)", poll_seconds)
    try:
        while True:
            try:
                status = await run_in_threadpool(_flush_once, database)
                if status.startswith("pushed"):   # quiet on the idle/not-configured ticks
                    log.info("devmgmt outbox flush: %s", status)
            except Exception:  # config/cert/DB error — log and keep looping (never kill the loop)
                log.exception("devmgmt sync tick error")
            await asyncio.sleep(poll_seconds)
    except asyncio.CancelledError:
        log.info("devmgmt outbox sync loop stopped")
        raise
