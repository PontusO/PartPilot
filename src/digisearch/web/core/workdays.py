"""Working-day (Mon-Fri) date arithmetic for production scheduling.

Durations and lead times are counted in working days so multi-week runs land realistically.
Dates flow through the app as ISO strings ("YYYY-MM-DD"); these helpers parse/return ``date``
objects and there's an ``iso()`` to format back. Company holidays are a future refinement.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta


def parse_date(value) -> date | None:
    """Accept an ISO string, a date/datetime, or None/'' → a date (or None)."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def _is_workday(d: date) -> bool:
    return d.weekday() < 5  # Mon=0 .. Fri=4


def add_workdays(value, n) -> date | None:
    """The date ``n`` working days after ``value`` (n>=0; weekends skipped)."""
    d = parse_date(value)
    if d is None:
        return None
    n = int(n or 0)
    while n > 0:
        d += timedelta(days=1)
        if _is_workday(d):
            n -= 1
    return d


def sub_workdays(value, n) -> date | None:
    """The date ``n`` working days before ``value`` (n>=0; weekends skipped)."""
    d = parse_date(value)
    if d is None:
        return None
    n = int(n or 0)
    while n > 0:
        d -= timedelta(days=1)
        if _is_workday(d):
            n -= 1
    return d


def workdays_between(a, b) -> int:
    """Number of working days in the half-open interval (a, b]; inverse of add_workdays.

    workdays_between(a, add_workdays(a, n)) == n for n >= 0.
    """
    a, b = parse_date(a), parse_date(b)
    if a is None or b is None or b <= a:
        return 0
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        if _is_workday(d):
            n += 1
    return n
