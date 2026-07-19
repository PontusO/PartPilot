from pathlib import Path

from digisearch.config import Settings


def test_settings_defaults():
    s = Settings()
    assert s.build_qty == 1
    assert s.reel_threshold == 10000.0


def test_settings_load_operational_defaults(tmp_path: Path):
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(
        "build_qty: 100\n"
        "currency: SEK\n"
        "reel_threshold: 5000\n"
        "confidence_threshold: 0.8\n"
    )
    s = Settings.load(cfg)
    assert s.build_qty == 100
    assert s.currency == "SEK"
    assert s.reel_threshold == 5000
    assert s.confidence_threshold == 0.8


def test_settings_load_missing_file_uses_defaults(tmp_path: Path):
    s = Settings.load(tmp_path / "nope.yaml")
    assert s.build_qty == 1


def test_scratch_db_copies_live_db_and_isolates_writes(tmp_path, monkeypatch):
    """`serve --scratch-db` runs against a throwaway copy, leaving the real DB untouched."""
    import os
    import shutil
    import sqlite3

    from digisearch.cli import _make_scratch_db

    live = tmp_path / "data" / "partpilot.db"
    live.parent.mkdir()
    conn = sqlite3.connect(live)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (42)")
    conn.commit()
    conn.close()

    monkeypatch.setenv("PARTPILOT_DB", str(live))
    monkeypatch.delenv("PARTPILOT_DATA_DIR", raising=False)

    scratch_dir, scratch_db = _make_scratch_db()
    try:
        assert scratch_db.exists() and scratch_db != live
        # the copy carries the live data, and the env now points the app at it
        with sqlite3.connect(scratch_db) as c:
            assert c.execute("SELECT x FROM t").fetchone()[0] == 42
        assert os.environ["PARTPILOT_DB"] == str(scratch_db)
        assert os.environ["PARTPILOT_DATA_DIR"] == str(scratch_dir)

        # writing to the scratch copy must not touch the live database
        with sqlite3.connect(scratch_db) as c:
            c.execute("INSERT INTO t VALUES (99)")
        with sqlite3.connect(live) as c:
            assert c.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)
