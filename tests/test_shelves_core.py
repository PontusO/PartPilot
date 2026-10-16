import pytest

from partpilot.web.core import FeatureRegistry
from partpilot.web.core.db import Database
from partpilot.web.features.catalog import feature as catalog_feature
from partpilot.web.features.catalog import repo as catrepo
from partpilot.web.features.catalog import stock


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "shelves.db")
    reg = FeatureRegistry()
    reg.register(catalog_feature)
    database.apply_migrations(reg)
    return database


def _part(db, part_no="R-1", qty=0):
    return catrepo.create_part(db, part={"part_no": part_no}, supplier_lines=[],
                               opening={"qty": qty} if qty else None)


def _shelf(db, name, kind="ours"):
    with db.connect() as conn:
        sid = conn.execute("INSERT INTO stock_locations (name, kind) VALUES (?, ?)",
                           (name, kind)).lastrowid
        conn.commit()
    return sid


def _main(db):
    with db.connect() as conn:
        return stock.default_shelf(conn)


def test_migration_adds_shelf_columns_and_main_is_ours(db):
    with db.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(stock_locations)")}
        assert {"kind", "owner_contact_id", "customer_order_id", "created_at", "retired_at"} <= cols
        pcols = {r["name"] for r in conn.execute("PRAGMA table_info(parts)")}
        assert "customer_qty" in pcols
        main = stock.default_shelf(conn)
        assert conn.execute("SELECT kind FROM stock_locations WHERE id = ?",
                            (main,)).fetchone()["kind"] == "ours"


def test_movement_on_new_shelf_creates_row_and_leaves_other_shelf_alone(db):
    pid = _part(db, qty=100)                    # 100 on Main (OPENING)
    cust = _shelf(db, "Acme", kind="customer")
    with db.connect() as conn:
        after = stock.post_movement(conn, pid, delta=-40, mtype=stock.ISSUE, location_id=cust)
        conn.commit()
        rows = {r["location_id"]: r["on_hand"] for r in conn.execute(
            "SELECT location_id, on_hand FROM part_stock WHERE part_id = ?", (pid,))}
    assert after == -40                          # the shelf's own balance, allowed negative
    assert rows[cust] == -40
    assert rows[_main(db)] == 100                # Main untouched: no fallback


def test_rollup_splits_ours_and_customer_totals(db):
    pid = _part(db, qty=100)
    cust = _shelf(db, "Acme", kind="customer")
    with db.connect() as conn:
        stock.post_movement(conn, pid, delta=25, mtype=stock.RECEIVE, location_id=cust)
        conn.commit()
        p = conn.execute("SELECT total_qty, customer_qty FROM parts WHERE id = ?", (pid,)).fetchone()
    assert p["total_qty"] == 100 and p["customer_qty"] == 25


def test_movement_without_shelf_is_refused(db):
    pid = _part(db)
    with db.connect() as conn, pytest.raises(ValueError):
        stock.post_movement(conn, pid, delta=1, mtype=stock.ADJUST, location_id=None)


def test_ledger_qty_after_is_shelf_balance(db):
    pid = _part(db, qty=10)
    cust = _shelf(db, "Acme", kind="customer")
    with db.connect() as conn:
        stock.post_movement(conn, pid, delta=5, mtype=stock.RECEIVE, location_id=cust)
        conn.commit()
        m = conn.execute("SELECT qty_after FROM stock_movements WHERE part_id = ? "
                         "ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
    assert m["qty_after"] == 5


def test_free_on_shelf_and_recompute_alloc(db):
    pid = _part(db, qty=10)
    cust = _shelf(db, "Acme", kind="customer")
    with db.connect() as conn:
        main = stock.default_shelf(conn)
        stock.post_movement(conn, pid, delta=7, mtype=stock.RECEIVE, location_id=cust)
        # allocations lives in customer_orders; a stand-in table is enough for this unit test.
        conn.execute("CREATE TABLE IF NOT EXISTS allocations (id INTEGER PRIMARY KEY, "
                     "customer_order_line_id INTEGER, part_id INTEGER, qty REAL, location_id INTEGER)")
        conn.execute("INSERT INTO allocations (customer_order_line_id, part_id, qty, location_id) "
                     "VALUES (1, ?, 3, ?)", (pid, cust))
        conn.execute("INSERT INTO allocations (customer_order_line_id, part_id, qty, location_id) "
                     "VALUES (1, ?, 4, ?)", (pid, main))
        stock.recompute_alloc(conn, pid)
        conn.commit()
        assert stock.free_on_shelf(conn, pid, cust) == 4      # 7 - 3
        assert stock.free_on_shelf(conn, pid, main) == 6      # 10 - 4
        assert conn.execute("SELECT total_alloc FROM parts WHERE id = ?",
                            (pid,)).fetchone()[0] == 4        # ours only
