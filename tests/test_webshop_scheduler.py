from datetime import datetime, timedelta

import pytest

from digisearch.web.core import FeatureRegistry
from digisearch.web.core.db import Database
from digisearch.web.features.setup import feature as setup_feature
from digisearch.web.features.setup import repo
from digisearch.web.features.setup.scheduler import _is_due


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "setup.db")
    reg = FeatureRegistry()
    reg.register(setup_feature)
    database.apply_migrations(reg)
    return database


def _at(hour, minute=0):
    return datetime(2026, 6, 26, hour, minute, 0)


def test_not_due_when_time_unset():
    assert _is_due("", "", _at(5)) is False
    assert _is_due(None, "", _at(5)) is False


def test_not_due_before_scheduled_time():
    assert _is_due("05:00", "", _at(4, 59)) is False


def test_due_at_scheduled_time_when_never_run():
    assert _is_due("05:00", "", _at(5, 0)) is True
    assert _is_due("05:00", "", _at(5, 30)) is True   # within the catch-up grace window


def test_not_due_after_grace_window():
    # daemon was down at 05:00 and only checks at 06:30 — don't sync in the workday
    assert _is_due("05:00", "", _at(6, 30)) is False


def test_not_due_again_once_run_today():
    ran = _at(5, 0).isoformat()
    assert _is_due("05:00", ran, _at(5, 1)) is False


def test_due_when_last_run_was_yesterday():
    yesterday = (_at(5, 0) - timedelta(days=1)).isoformat()
    assert _is_due("05:00", yesterday, _at(5, 0)) is True


def test_due_when_timestamp_unparseable():
    assert _is_due("05:00", "not-a-date", _at(5, 0)) is True


def test_time_round_trip_and_disable(db):
    repo.set_webshop_time(db, "5:00")                       # normalized to zero-padded
    assert repo.get_webshop(db)["sync_at_time"] == "05:00"
    repo.set_webshop_time(db, "23:45")
    assert repo.get_webshop(db)["sync_at_time"] == "23:45"

    repo.set_webshop_time(db, "")                           # blank off
    assert repo.get_webshop(db)["sync_at_time"] == ""
    repo.set_webshop_time(db, "garbage")                    # unparseable off
    assert repo.get_webshop(db)["sync_at_time"] == ""
    repo.set_webshop_time(db, "25:00")                      # out of range off
    assert repo.get_webshop(db)["sync_at_time"] == ""


def test_auto_status_recorded(db):
    repo.set_webshop_auto_status(db, "2026-06-26T12:00:00", "ok: 3 updated")
    data = repo.get_webshop(db)
    assert data["last_auto_sync_at"] == "2026-06-26T12:00:00"
    assert data["last_auto_sync_status"] == "ok: 3 updated"
