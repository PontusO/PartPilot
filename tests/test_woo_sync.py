import pytest

from digisearch.web.core import FeatureRegistry
from digisearch.web.core.db import Database
from digisearch.web.features.catalog import feature as catalog_feature
from digisearch.web.features.catalog import repo, stock, woo_sync
from digisearch.woocommerce import WooProduct


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "cat.db")
    reg = FeatureRegistry()
    reg.register(catalog_feature)
    database.apply_migrations(reg)
    return database


def _p(sku, qty=5, manage=True, name="Thing", wid=1):
    return WooProduct(id=wid, sku=sku, name=name, description=None,
                      stock_quantity=(qty if manage else None),
                      manage_stock=manage, stock_status="instock", type="simple")


def _make_part(db, part_no, qty):
    return repo.create_part(db, part={"part_no": part_no}, supplier_lines=[],
                            opening={"qty": qty})


class FakePush:
    """Captures the (woo_id, qty) batch handed to the client, no network."""
    def __init__(self):
        self.calls = []

    def update_stock_batch(self, updates):
        updates = list(updates)
        self.calls.append(updates)
        return len(updates)


def _baseline(db, part_no):
    return repo.find_part_by_part_no(db, part_no)["webshop_synced_qty"]


def test_existing_part_stock_adjusted_to_woo(db):
    pid = _make_part(db, "99-100", 3)
    report = woo_sync.sync_from_woo(db, [_p("99-100", qty=10)], user="bob")
    assert report.updated == 1 and report.matched == 1
    assert repo.get_part(db, pid)["total_qty"] == 10
    # one ADJUST movement recording the change
    with db.connect() as conn:
        moves = conn.execute(
            "SELECT mtype, qty_delta, reference FROM stock_movements WHERE part_id=?", (pid,)
        ).fetchall()
    assert [m["mtype"] for m in moves] == ["ADJUST"]
    assert moves[0]["qty_delta"] == 7 and moves[0]["reference"] == "woo-sync"


def test_equal_stock_writes_no_movement(db):
    pid = _make_part(db, "99-100", 8)
    report = woo_sync.sync_from_woo(db, [_p("99-100", qty=8)])
    assert report.unchanged == 1 and report.updated == 0
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM stock_movements WHERE part_id=?", (pid,)).fetchone()[0]
    assert n == 0


def test_missing_component_is_created(db):
    report = woo_sync.sync_from_woo(db, [_p("99-555", qty=12, name="Cap")])
    assert report.created_parts == 1 and report.created_assemblies == 0
    part = repo.find_part_by_part_no(db, "99-555")
    assert part["kind"] == "PART" and part["value"] == "Cap" and part["total_qty"] == 12


def test_missing_assembly_is_created_with_stock(db):
    report = woo_sync.sync_from_woo(db, [_p("98-1", qty=4, name="Board")])
    assert report.created_assemblies == 1
    part = repo.find_part_by_part_no(db, "98-1")
    assert part["kind"] == "ASSY" and part["total_qty"] == 4
    with db.connect() as conn:
        mtype = conn.execute(
            "SELECT mtype FROM stock_movements WHERE part_id=?", (part["id"],)).fetchone()["mtype"]
    assert mtype == "OPENING"


def test_unknown_prefix_is_skipped(db):
    report = woo_sync.sync_from_woo(db, [_p("12-345")])
    assert report.skipped == 1 and report.created == 0
    assert repo.find_part_by_part_no(db, "12-345") is None


def test_unmanaged_stock_leaves_part_untouched(db):
    pid = _make_part(db, "99-100", 9)
    report = woo_sync.sync_from_woo(db, [_p("99-100", manage=False)])
    assert report.unmanaged == 1 and report.updated == 0
    assert repo.get_part(db, pid)["total_qty"] == 9


def test_dry_run_writes_nothing(db):
    _make_part(db, "99-100", 3)
    report = woo_sync.sync_from_woo(
        db, [_p("99-100", qty=10), _p("99-999", qty=2), _p("98-9", qty=1)], dry_run=True)
    assert report.updated == 1 and report.created_parts == 1 and report.created_assemblies == 1
    # nothing actually created or moved
    assert repo.find_part_by_part_no(db, "99-999") is None
    assert repo.get_part(db, repo.find_part_by_part_no(db, "99-100")["id"])["total_qty"] == 3
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0] == 0


def test_two_way_sale_and_build_reconciles_and_pushes(db):
    pid = _make_part(db, "99-1", 120)
    woo_sync.sync_from_woo(db, [_p("99-1", qty=120, wid=50)])     # first sync sets baseline=120
    assert _baseline(db, "99-1") == 120

    # webshop sells 20 (Woo 120->100); PartPilot builds 100 (120->220 via a BUILD movement)
    stock.adjust_stock(db, pid, delta=100, mtype=stock.BUILD, reference="WO-1")

    client = FakePush()
    report = woo_sync.sync_from_woo(db, [_p("99-1", qty=100, wid=50)], client=client, user="bob")

    assert report.sold == 20 and report.pushed == 1
    part = repo.get_part(db, pid)
    assert part["total_qty"] == 200                  # 220 built - 20 sold
    assert part["webshop_synced_qty"] == 200         # baseline advanced to pushed value
    assert client.calls == [[(50, 200.0)]]           # pushed R=200 to woo product 50
    # the sale is recorded in the ledger
    with db.connect() as conn:
        mt = conn.execute("SELECT mtype, reference FROM stock_movements WHERE part_id=? "
                          "ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
    assert mt["mtype"] == "ISSUE" and mt["reference"] == "woo-sale"


def test_build_only_pushes_without_touching_partpilot(db):
    pid = _make_part(db, "99-2", 50)
    woo_sync.sync_from_woo(db, [_p("99-2", qty=50, wid=7)])       # baseline=50
    stock.adjust_stock(db, pid, delta=80, mtype=stock.BUILD, reference="WO")   # built 80 -> 130

    client = FakePush()
    report = woo_sync.sync_from_woo(db, [_p("99-2", qty=50, wid=7)], client=client)

    assert report.pushed == 1 and report.updated == 0 and report.sold == 0
    assert client.calls == [[(7, 130.0)]]
    part = repo.get_part(db, pid)
    assert part["total_qty"] == 130 and part["webshop_synced_qty"] == 130


def test_sale_only_issues_without_push(db):
    pid = _make_part(db, "99-3", 50)
    woo_sync.sync_from_woo(db, [_p("99-3", qty=50, wid=9)])       # baseline=50
    client = FakePush()
    report = woo_sync.sync_from_woo(db, [_p("99-3", qty=30, wid=9)], client=client)

    assert report.sold == 20 and report.pushed == 0 and client.calls == []
    part = repo.get_part(db, pid)
    assert part["total_qty"] == 30 and part["webshop_synced_qty"] == 30


def test_two_way_dry_run_pushes_nothing(db):
    pid = _make_part(db, "99-4", 100)
    woo_sync.sync_from_woo(db, [_p("99-4", qty=100, wid=3)])      # baseline=100
    stock.adjust_stock(db, pid, delta=80, mtype=stock.BUILD, reference="WO")   # built 80 -> 180

    client = FakePush()
    report = woo_sync.sync_from_woo(db, [_p("99-4", qty=80, wid=3)],   # also sold 20
                                    client=client, dry_run=True)
    # reports what would happen...
    assert report.sold == 20 and report.pushed == 1
    # ...but writes nothing and pushes nothing
    assert client.calls == []
    part = repo.get_part(db, pid)
    assert part["total_qty"] == 180 and part["webshop_synced_qty"] == 100


def test_push_failure_keeps_partpilot_and_baseline_safe(db):
    pid = _make_part(db, "99-5", 50)
    woo_sync.sync_from_woo(db, [_p("99-5", qty=50, wid=4)])       # baseline=50
    stock.adjust_stock(db, pid, delta=40, mtype=stock.BUILD, reference="WO")   # built 40 -> 90

    class Boom:
        def update_stock_batch(self, updates):
            raise RuntimeError("woo down")

    report = woo_sync.sync_from_woo(db, [_p("99-5", qty=50, wid=4)], client=Boom())
    assert report.pushed == 0 and len(report.errors) == 1
    # PartPilot on-hand stands; baseline stays at Woo's value so the push retries next time
    part = repo.get_part(db, pid)
    assert part["total_qty"] == 90 and part["webshop_synced_qty"] == 50


def test_one_bad_product_does_not_abort_run(db):
    _make_part(db, "99-1", 3)  # exists -> goes through the stock-update path
    bad = WooProduct(id=1, sku="99-1", name="x", description=None, stock_quantity="oops",  # type: ignore
                     manage_stock=True, stock_status=None, type="simple")
    report = woo_sync.sync_from_woo(db, [bad, _p("99-2", qty=5)])
    assert len(report.errors) == 1 and report.errors[0]["sku"] == "99-1"
    assert report.created_parts == 1  # the good one still ran
    assert repo.find_part_by_part_no(db, "99-2") is not None
