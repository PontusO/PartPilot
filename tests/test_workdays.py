from datetime import date, timedelta

from digisearch.web.core import add_workdays, iso, parse_date, sub_workdays, workdays_between


def _weekday_on_or_after(d, wd):  # wd: 0=Mon .. 4=Fri
    while d.weekday() != wd:
        d += timedelta(days=1)
    return d


def test_add_workdays_skips_weekends():
    fri = _weekday_on_or_after(date(2026, 6, 1), 4)
    nxt = add_workdays(fri, 1)
    assert nxt == fri + timedelta(days=3) and nxt.weekday() == 0   # Fri +1 working day -> Mon
    assert add_workdays(fri, 0) == fri
    assert add_workdays("2026-06-01", 5).weekday() < 5             # always lands on a weekday


def test_sub_workdays_skips_weekends():
    mon = _weekday_on_or_after(date(2026, 6, 1), 0)
    assert sub_workdays(mon, 1) == mon - timedelta(days=3)         # Mon -1 working day -> Fri


def test_workdays_between_is_inverse_of_add():
    mon = _weekday_on_or_after(date(2026, 6, 1), 0)
    for n in range(0, 18):
        assert workdays_between(mon, add_workdays(mon, n)) == n


def test_parse_and_iso():
    assert iso(parse_date("2026-06-01")) == "2026-06-01"
    assert parse_date(None) is None and parse_date("") is None
    assert parse_date(date(2026, 6, 1)) == date(2026, 6, 1)
