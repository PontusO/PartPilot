"""Home page helpers: the newest / last-edited lists each stat card shows."""
import sqlite3


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT, dead INTEGER DEFAULT 0, "
                 "created_at TEXT, updated_at TEXT)")
    for i in range(1, 7):                              # created on Jan 1..6, never edited
        conn.execute("INSERT INTO t VALUES (?, ?, 0, ?, ?)",
                     (i, f"n{i}", f"2026-01-0{i} 09:00:00", f"2026-01-0{i} 09:00:00"))
    conn.execute("UPDATE t SET updated_at = '2026-02-02 10:00:00' WHERE id = 2")   # edited
    conn.execute("UPDATE t SET updated_at = '2026-02-03 10:00:00' WHERE id = 1")   # edited later
    conn.execute("UPDATE t SET dead = 1 WHERE id = 6")                             # filtered out
    return conn


def test_recent_entries_newest_by_creation_and_edited_by_update():
    from partpilot.web.core.home import recent_entries
    newest, edited = recent_entries(_conn(), table="t", label="name", url="/t/", where="dead = 0")
    assert [e.label for e in newest] == ["n5", "n4", "n3", "n2"]       # 4 newest, row 6 hidden
    assert newest[0].url == "/t/5" and newest[0].when == "2026-01-05"
    assert [e.label for e in edited] == ["n1", "n2"]                   # only rows edited after creation
    assert edited[0].when == "2026-02-03"


def test_recent_entries_limit_and_custom_key():
    from partpilot.web.core.home import recent_entries
    newest, _ = recent_entries(_conn(), table="t", label="'row ' || id", url="/x?id=", key="name", limit=2)
    assert [(e.label, e.url) for e in newest] == [("row 6", "/x?id=n6"), ("row 5", "/x?id=n5")]


def test_recent_entries_tone_expression_colours_each_row():
    from partpilot.web.core.home import recent_entries
    conn = _conn()
    conn.execute("ALTER TABLE t ADD COLUMN status TEXT DEFAULT 'open'")
    conn.execute("UPDATE t SET status = 'done' WHERE id = 5")
    conn.execute("UPDATE t SET status = 'cancelled' WHERE id = 4")
    newest, _ = recent_entries(conn, table="t", label="name", url="/t/", where="dead = 0",
                               tone="CASE status WHEN 'done' THEN 'ok' WHEN 'cancelled' THEN 'bad' ELSE 'accent' END")
    assert [e.tone for e in newest] == ["ok", "bad", "accent", "accent"]
    plain, _ = recent_entries(conn, table="t", label="name", url="/t/")
    assert plain[0].tone is None                        # no tone expression → uncoloured
