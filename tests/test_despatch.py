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
    # confirmed — despatch requires it (a draft order can no longer be packed & shipped)
    oid = corepo.create_order(db, {"customer_id": cust, "order_ref": "SO-D", "status": "confirmed"})
    corepo.add_line(db, oid, part, qty, 10.0, None)
    return cust, part, oid


def _pack_and_dispatch(db, desp_id, user="u"):
    """Helper: check off every line, confirm ready, and dispatch."""
    all_lines = {ln["id"] for ln in repo.get_despatch(db, desp_id)["lines"]}
    repo.set_packing(db, desp_id, all_lines)
    repo.confirm_packed(db, desp_id, user)
    repo.dispatch(db, desp_id, user)


def test_packing_list_moves_no_stock_until_dispatched(db):
    cust, part, oid = _order(db, qty=20, stock_qty=50)
    corepo.allocate_order(db, oid)                    # reserve 20
    line_id = corepo.get_order(db, oid)["lines"][0]["id"]

    desp_id = repo.create_packing_list(db, oid, {line_id: 12}, "u")
    assert desp_id is not None
    d = repo.get_despatch(db, desp_id)
    assert d["status"] == "packing" and d["all_packed"] is False
    # nothing has moved yet: stock, allocation and shipped_qty untouched
    p = catrepo.get_part(db, part)
    assert p["total_qty"] == 50 and p["total_alloc"] == 20
    assert corepo.get_order(db, oid)["lines"][0]["shipped_qty"] == 0
    assert not [m for m in cstock.movements_for_part(db, part) if m["mtype"] != "OPENING"]


def test_dispatch_ships_stock_and_consumes_allocation(db):
    cust, part, oid = _order(db, qty=20, stock_qty=50)
    corepo.allocate_order(db, oid)                    # reserve 20
    line_id = corepo.get_order(db, oid)["lines"][0]["id"]
    desp_id = repo.create_packing_list(db, oid, {line_id: 12}, "u")
    _pack_and_dispatch(db, desp_id)

    p = catrepo.get_part(db, part)
    assert p["total_qty"] == 38                       # 50 − 12 shipped out
    assert p["total_alloc"] == 8                      # 20 − 12 allocation consumed
    assert corepo.get_order(db, oid)["lines"][0]["shipped_qty"] == 12

    d = repo.get_despatch(db, desp_id)
    assert d["status"] == "open" and d["despatch_date"]
    assert d["lines"][0]["qty"] == 12 and d["lines"][0]["part_id"] == part
    assert abs(d["lines"][0]["unit_price"] - 10.0) < 1e-9
    assert any(m["mtype"] == "ISSUE" for m in cstock.movements_for_part(db, part))


def test_cannot_confirm_until_every_line_packed(db):
    cust, part, oid = _order(db, qty=20, stock_qty=50)
    corepo.add_line(db, oid, part, 5, 10.0, None)     # a second line
    line_ids = [ln["id"] for ln in corepo.get_order(db, oid)["lines"]]
    desp_id = repo.create_packing_list(db, oid, {line_ids[0]: 2, line_ids[1]: 3}, "u")

    desp_lines = repo.get_despatch(db, desp_id)["lines"]
    repo.set_packing(db, desp_id, {desp_lines[0]["id"]})   # only one of two packed
    with pytest.raises(ValueError):
        repo.confirm_packed(db, desp_id, "u")
    assert repo.get_despatch(db, desp_id)["status"] == "packing"

    repo.set_packing(db, desp_id, {ln["id"] for ln in desp_lines})  # pack both
    repo.confirm_packed(db, desp_id, "u")
    assert repo.get_despatch(db, desp_id)["status"] == "packed"


def test_cannot_dispatch_before_confirming_ready(db):
    cust, part, oid = _order(db)
    line_id = corepo.get_order(db, oid)["lines"][0]["id"]
    desp_id = repo.create_packing_list(db, oid, {line_id: 5}, "u")
    with pytest.raises(ValueError):
        repo.dispatch(db, desp_id, "u")               # still 'packing'
    assert not [m for m in cstock.movements_for_part(db, part)
                if m["mtype"] != "OPENING"]    # nothing shipped


def test_reopen_and_cancel_packing(db):
    cust, part, oid = _order(db)
    line_id = corepo.get_order(db, oid)["lines"][0]["id"]
    desp_id = repo.create_packing_list(db, oid, {line_id: 5}, "u")
    repo.set_packing(db, desp_id, {ln["id"] for ln in repo.get_despatch(db, desp_id)["lines"]})
    repo.confirm_packed(db, desp_id, "u")
    repo.reopen_packing(db, desp_id)
    assert repo.get_despatch(db, desp_id)["status"] == "packing"

    assert repo.cancel_packing(db, desp_id) == oid
    assert repo.get_despatch(db, desp_id) is None      # discarded, no stock moved
    assert not [m for m in cstock.movements_for_part(db, part) if m["mtype"] != "OPENING"]


def test_full_despatch_marks_order_shipped(db):
    cust, part, oid = _order(db, qty=20, stock_qty=50)
    line_id = corepo.get_order(db, oid)["lines"][0]["id"]
    desp_id = repo.create_packing_list(db, oid, {line_id: 20}, "u")
    _pack_and_dispatch(db, desp_id)
    assert corepo.get_order(db, oid)["status"] == "shipped"
    assert [ln for ln in repo.shippable_lines(db, oid) if ln["outstanding"] > 0] == []


def test_invoice_a_despatch(db):
    cust, part, oid = _order(db)
    line_id = corepo.get_order(db, oid)["lines"][0]["id"]
    desp_id = repo.create_packing_list(db, oid, {line_id: 5}, "u")
    _pack_and_dispatch(db, desp_id)
    repo.mark_invoiced(db, desp_id, "INV-100", "2026-06-07")
    d = repo.get_despatch(db, desp_id)
    assert d["status"] == "invoiced" and d["invoice_no"] == "INV-100"


def test_shippable_qty_capped_at_stock(db):
    cust, part, oid = _order(db, qty=30, stock_qty=10)
    s = repo.shippable_lines(db, oid)[0]
    assert s["outstanding"] == 30 and s["suggested_qty"] == 10


def test_draft_order_cannot_open_packing_list(db):
    cust, part, oid = _order(db)
    with db.connect() as conn:  # back to draft — despatch must refuse
        conn.execute("UPDATE customer_orders SET status = 'draft' WHERE id = ?", (oid,))
        conn.commit()
    line_id = corepo.get_order(db, oid)["lines"][0]["id"]
    with pytest.raises(ValueError, match="confirmed"):
        repo.create_packing_list(db, oid, {line_id: 5}, "u")


def test_second_packing_list_capped_at_remaining(db):
    # Two packing lists must never together exceed the ordered qty (double-click / two operators).
    cust, part, oid = _order(db, qty=20, stock_qty=50)
    line_id = corepo.get_order(db, oid)["lines"][0]["id"]
    repo.create_packing_list(db, oid, {line_id: 15}, "u")   # 15 of 20 now on an open list
    assert repo.shippable_lines(db, oid)[0]["outstanding"] == 5   # open list subtracted
    with pytest.raises(ValueError, match="exceeds"):
        repo.create_packing_list(db, oid, {line_id: 6}, "u")      # 6 > the 5 remaining
    d2 = repo.create_packing_list(db, oid, {line_id: 5}, "u")     # exactly the rest is fine
    assert d2 is not None


def test_invoicing_last_despatch_completes_the_order(db):
    cust, part, oid = _order(db, qty=20, stock_qty=50)
    line_id = corepo.get_order(db, oid)["lines"][0]["id"]
    desp_id = repo.create_packing_list(db, oid, {line_id: 20}, "u")
    _pack_and_dispatch(db, desp_id)
    assert corepo.get_order(db, oid)["status"] == "shipped"
    repo.mark_invoiced(db, desp_id, "INV-1", None)
    assert corepo.get_order(db, oid)["status"] == "complete"  # fully shipped + all invoiced
