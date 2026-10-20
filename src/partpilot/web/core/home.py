"""Home page building blocks shared by every feature's ``home_stats`` hook.

Each stat card lists the newest rows and the most recently edited rows of the table it counts.
``recent_entries`` produces both lists from any table that carries the conventional
``created_at`` / ``updated_at`` columns, so features only name the table, the label expression and
the detail-page URL prefix — the ordering and the "edited means updated after creation" rule live
here once.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

RECENT_SHOWN = 4


@dataclass(frozen=True)
class HomeEntry:
    """One row in a card's New / Edited list: a link with the date it was created or edited."""

    label: str
    url: str
    when: str                 # ISO date (YYYY-MM-DD)
    tone: str | None = None   # colour token: accent (in progress) | ok | warn | bad | muted, or None


def recent_entries(
    conn: sqlite3.Connection,
    *,
    table: str,
    label: str,
    url: str,
    where: str = "1 = 1",
    key: str = "id",
    tone: str | None = None,
    limit: int = RECENT_SHOWN,
) -> tuple[tuple[HomeEntry, ...], tuple[HomeEntry, ...]]:
    """Return ``(newest, edited)`` for ``table``.

    ``table`` is a table name or a parenthesised subquery (to join in a related name — see the
    purchase_orders and work_orders hooks); it must expose ``id``, ``created_at`` and ``updated_at``.
    ``label`` and ``key`` are SQL expressions; ``url`` is a prefix the key is appended to
    (``"/catalog/"`` → ``/catalog/42``). ``newest`` is ordered by ``created_at`` descending; ``edited``
    holds only rows whose ``updated_at`` is later than their ``created_at``, newest edit first. A row
    can appear in both lists. ``where`` narrows both (retired rows, a contact kind, ...). ``tone``,
    when given, is a SQL expression yielding a colour token per row (``'accent'``/``'ok'``/``'bad'``…),
    typically a ``CASE status …`` — the template colours the entry's link with it.
    """

    def fetch(order_col: str, extra: str) -> tuple[HomeEntry, ...]:
        rows = conn.execute(
            f"SELECT {label}, {key}, {order_col}, {tone or 'NULL'} FROM {table} WHERE ({where}){extra} "
            f"ORDER BY {order_col} DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return tuple(HomeEntry(str(r[0]), f"{url}{r[1]}", str(r[2] or "")[:10], r[3]) for r in rows)

    return (fetch("created_at", ""), fetch("updated_at", " AND updated_at > created_at"))
