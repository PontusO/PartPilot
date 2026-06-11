import pytest

from digisearch.web.core import FeatureRegistry
from digisearch.web.core.db import Database
from digisearch.web.features.catalog import feature as catalog_feature
from digisearch.web.features.catalog import repo as catrepo
from digisearch.web.features.catalog import stock as cstock
from digisearch.web.features.contacts import feature as contacts_feature
from digisearch.web.features.contacts import repo as conrepo
from digisearch.web.features.customer_orders import feature as co_feature
from digisearch.web.features.customer_orders import repo as corepo
from digisearch.web.features.despatch import feature as desp_feature
from digisearch.web.features.despatch import repo


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "d.db")
    reg = FeatureRegistry()
    reg.register(catalog_feature, contacts_feature, co_feature, desp_feature)
    database.apply_migrations(reg)
    return database


def _order(db, qty=20, stock_qty=50):
    cust = conrepo.create_contact(db, {"kind": "customer", "name": "Acme"})
    part = catrepo.create_part(
        db, part={"part_no": "D-1"},
        supplier_lines=[{"supplier_name": "X", "unit_price": 2.0, "reel_qty": 1, "is_default": True}],
        opening={"qty": stock_qty})
    oid = corepo.create_order(db, {"customer_id": cust, "order_ref": "SO-D"})
    corepo.add_line(db, oid, part, qty, 10.0, None)
    return cust, part, oid


def test_despatch_ships_stock_and_consumes_allocation(db):
    cust, part, oid = _order(db, qty=20, stock_qty=50)
    corepo.allocate_order(db, oid)                    # reserve 20
    assert catrepo.get_part(db, part)["total_alloc"] == 20
    line_id = corepo.get_order(db, oid)["lines"][0]["id"]

    desp_id = repo.create_despatch(db, oid, {line_id: 12}, "u")
    assert desp_id is not None
    p = catrepo.get_part(db, part)
    assert p["total_qty"] == 38                       # 50 − 12 shipped out
    assert p["total_alloc"] == 8                      # 20 − 12 allocation consumed
    assert corepo.get_order(db, oid)["lines"][0]["shipped_qty"] == 12

    d = repo.get_despatch(db, desp_id)
    assert d["lines"][0]["qty"] == 12 and d["lines"][0]["part_id"] == part
    assert abs(d["lines"][0]["unit_price"] - 10.0) < 1e-9
    assert any(m["mtype"] == "ISSUE" for m in cstock.movements_for_part(db, part))


def test_full_despatch_marks_order_shipped(db):
    cust, part, oid = _order(db, qty=20, stock_qty=50)
    line_id = corepo.get_order(db, oid)["lines"][0]["id"]
    repo.create_despatch(db, oid, {line_id: 20}, "u")
    assert corepo.get_order(db, oid)["status"] == "shipped"
    assert [ln for ln in repo.shippable_lines(db, oid) if ln["outstanding"] > 0] == []


def test_invoice_a_despatch(db):
    cust, part, oid = _order(db)
    line_id = corepo.get_order(db, oid)["lines"][0]["id"]
    desp_id = repo.create_despatch(db, oid, {line_id: 5}, "u")
    repo.mark_invoiced(db, desp_id, "INV-100", "2026-06-07")
    d = repo.get_despatch(db, desp_id)
    assert d["status"] == "invoiced" and d["invoice_no"] == "INV-100"


def test_shippable_qty_capped_at_stock(db):
    cust, part, oid = _order(db, qty=30, stock_qty=10)
    s = repo.shippable_lines(db, oid)[0]
    assert s["outstanding"] == 30 and s["suggested_qty"] == 10
