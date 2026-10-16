from partpilot.web.core import Feature, FeatureRegistry, Migration
from partpilot.web.core.db import Database


def _registry_with_table():
    reg = FeatureRegistry()
    reg.register(
        Feature(
            name="catalog",
            migrations=[
                Migration(1, "create parts", "CREATE TABLE parts (id INTEGER PRIMARY KEY, mpn TEXT);"),
                Migration(2, "add stock col", "ALTER TABLE parts ADD COLUMN stock INTEGER DEFAULT 0;"),
            ],
        )
    )
    return reg


def test_migrations_apply_and_are_idempotent(tmp_path):
    db = Database(tmp_path / "p.db")
    reg = _registry_with_table()

    applied = db.apply_migrations(reg)
    assert applied == [("catalog", 1), ("catalog", 2)]

    # Table and both columns exist.
    with db.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(parts)")}
    assert {"id", "mpn", "stock"} <= cols

    # Running again applies nothing.
    assert db.apply_migrations(reg) == []


def test_only_pending_migrations_run(tmp_path):
    db = Database(tmp_path / "p.db")
    reg = _registry_with_table()
    db.apply_migrations(reg)

    # A new version on the same feature applies only that step.
    reg.features[0].migrations.append(
        Migration(3, "add index", "CREATE INDEX ix_parts_mpn ON parts(mpn);")
    )
    assert db.apply_migrations(reg) == [("catalog", 3)]


def test_duplicate_feature_rejected():
    reg = FeatureRegistry()
    reg.register(Feature(name="x"))
    try:
        reg.register(Feature(name="x"))
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_nav_filtered_by_role():
    from partpilot.web.core import NavItem

    reg = FeatureRegistry()
    reg.register(
        Feature(name="a", nav=NavItem("Purchasing", "/purchasing", roles=frozenset({"purchasing"}), order=10)),
        Feature(name="b", nav=NavItem("Stock", "/stock", roles=frozenset({"warehouse"}), order=20)),
        Feature(name="c", nav=NavItem("Home", "/home", roles=None, order=5)),
    )
    buyer = [n.url for n in reg.nav_for("purchasing")]
    assert buyer == ["/home", "/purchasing"]  # order respected, warehouse-only hidden
    assert [n.url for n in reg.nav_for(None)] == ["/home"]


# ---- callable migrations, pre-migration backup, rebuild_table (single-supplier-store plan Task 1) ----

import sqlite3

import pytest

from partpilot.web.core import Feature, FeatureRegistry, Migration
from partpilot.web.core.db import Database, rebuild_table, table_exists


def _feat(*migs):
    return Feature(name="t", migrations=list(migs))


def test_callable_migration_receives_connection(tmp_path):
    seen = {}

    def mig(conn):
        conn.execute("CREATE TABLE made (id INTEGER)")
        seen["ok"] = isinstance(conn, sqlite3.Connection)

    db = Database(tmp_path / "x.db")
    reg = FeatureRegistry(); reg.register(_feat(Migration(1, "callable", mig)))
    assert db.apply_migrations(reg) == [("t", 1)]
    assert seen["ok"]
    with db.connect() as conn:
        assert table_exists(conn, "made") and not table_exists(conn, "nope")
        assert conn.execute("SELECT 1 FROM schema_migrations WHERE feature='t' AND version=1").fetchone()


def test_backup_written_only_when_migrations_pending_and_not_first_run(tmp_path):
    bdir = tmp_path / "backups"
    db = Database(tmp_path / "x.db", backup_dir=bdir)
    reg = FeatureRegistry(); reg.register(_feat(Migration(1, "a", "CREATE TABLE a (id INTEGER);")))
    db.apply_migrations(reg)                     # first run: nothing to protect yet
    assert not bdir.exists() or not list(bdir.iterdir())
    db.apply_migrations(reg)                     # nothing pending
    assert not bdir.exists() or not list(bdir.iterdir())
    reg2 = FeatureRegistry()
    reg2.register(_feat(Migration(1, "a", "CREATE TABLE a (id INTEGER);"),
                        Migration(2, "b", "CREATE TABLE b (id INTEGER);")))
    db.apply_migrations(reg2)                    # pending on an existing DB -> backup
    files = list(bdir.glob("partpilot-*.db"))
    assert len(files) == 1
    with sqlite3.connect(files[0]) as c:          # the copy is a real database at the pre-state
        assert c.execute("SELECT name FROM sqlite_master WHERE name='a'").fetchone()
        assert not c.execute("SELECT name FROM sqlite_master WHERE name='b'").fetchone()


def test_backup_failure_aborts_before_migrating(tmp_path):
    blocker = tmp_path / "file.txt"; blocker.write_text("x")
    db = Database(tmp_path / "x.db", backup_dir=blocker / "backups")   # cannot mkdir under a file
    reg = FeatureRegistry(); reg.register(_feat(Migration(1, "a", "CREATE TABLE a (id INTEGER);")))
    db.apply_migrations(reg)                     # first run, no backup attempted
    reg2 = FeatureRegistry()
    reg2.register(_feat(Migration(1, "a", "CREATE TABLE a (id INTEGER);"),
                        Migration(2, "b", "CREATE TABLE b (id INTEGER);")))
    with pytest.raises(OSError):
        db.apply_migrations(reg2)
    with db.connect() as conn:
        assert not table_exists(conn, "b")        # migration 2 never ran


def test_rebuild_table_swaps_fk_and_keeps_rows_and_children(tmp_path):
    db = Database(tmp_path / "x.db")
    with db.connect() as conn:
        conn.executescript("""
            CREATE TABLE old_parent (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE new_parent (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES old_parent(id), note TEXT);
            CREATE INDEX ix_child_parent ON child(parent_id);
            CREATE TABLE grandchild (id INTEGER PRIMARY KEY, child_id INTEGER NOT NULL REFERENCES child(id) ON DELETE CASCADE);
            INSERT INTO old_parent VALUES (1,'a'),(2,'b');
            INSERT INTO new_parent VALUES (10,'a'),(20,'b');
            INSERT INTO child VALUES (1,1,'x'),(2,2,'y'),(3,NULL,'z');
            INSERT INTO grandchild VALUES (1,1),(2,2),(3,3);
        """)
        rebuild_table(conn, "child",
                      sql_replacements={"REFERENCES old_parent(id)": "REFERENCES new_parent(id)"},
                      column_exprs={"parent_id": "parent_id * 10"})
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='child'").fetchone()[0]
        assert "REFERENCES new_parent(id)" in sql and "old_parent" not in sql
        assert [tuple(r) for r in conn.execute("SELECT parent_id FROM child ORDER BY id")] == [(10,), (20,), (None,)]
        assert conn.execute("SELECT COUNT(*) FROM grandchild").fetchone()[0] == 3     # no cascade
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='ix_child_parent'").fetchone()
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert not conn.execute("PRAGMA foreign_key_check(child)").fetchall()


def test_rebuild_table_rolls_back_on_dangling_reference(tmp_path):
    db = Database(tmp_path / "x.db")
    with db.connect() as conn:
        conn.executescript("""
            CREATE TABLE new_parent (id INTEGER PRIMARY KEY);
            CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES new_parent(id));
            INSERT INTO new_parent VALUES (1);
            INSERT INTO child VALUES (1, 1), (2, 1);
        """)
        with pytest.raises(RuntimeError):
            rebuild_table(conn, "child", sql_replacements={}, column_exprs={"parent_id": "99"})
        assert [tuple(r) for r in conn.execute("SELECT parent_id FROM child ORDER BY id")] == [(1,), (1,)]
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name='child__new'").fetchone()
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
