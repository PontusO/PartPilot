import pytest

from partpilot.web.core import FeatureRegistry
from partpilot.web.core.db import Database
from partpilot.web.features.assemblies import feature as assemblies_feature
from partpilot.web.features.assemblies import repo as asmrepo
from partpilot.web.features.catalog import feature as catalog_feature
from partpilot.web.features.catalog import repo as catrepo
from partpilot.web.features.catalog import stock
from partpilot.web.features.contacts import feature as contacts_feature
from partpilot.web.features.contacts import repo as conrepo
from partpilot.web.features.customer_orders import feature as co_feature
from partpilot.web.features.customer_orders import repo as corepo
from partpilot.web.features.work_orders import feature as wo_feature
from partpilot.web.features.work_orders import repo


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "wo.db")
    reg = FeatureRegistry()
    reg.register(catalog_feature, assemblies_feature, contacts_feature, co_feature, wo_feature)
    database.apply_migrations(reg)
    return database


def _world(db):
    r1 = catrepo.create_part(
        db, part={"part_no": "R-1"},
        supplier_lines=[{"supplier_name": "X", "unit_price": 1, "reel_qty": 1, "is_default": True}],
        opening={"qty": 3000})
    asm = asmrepo.create_assembly(db, {"part_no": "PROD"})
    asmrepo.add_bom_line(db, asm, r1, 1, None)
    cust = conrepo.create_contact(db, {"kind": "customer", "name": "Acme"})
    shelf = catrepo.create_shelf(db, kind="customer", owner_contact_id=cust)
    return r1, asm, cust, shelf


def test_customer_wo_is_short_even_though_main_is_full(db):
    r1, asm, cust, shelf = _world(db)
    wo = repo.get_work_order(db, repo.create_work_order(
        db, {"assembly_id": asm, "qty": 100, "location_id": shelf}))
    ln = wo["lines"][0]
    assert ln["available"] == 0 and ln["short"] == 100 and wo["short_count"] == 1
    assert wo["shelf"]["name"] == "Acme" and wo["customer_supplied"] is True
    assert wo["loaded_build_cost_charged"] == 0
    assert wo["loaded_build_cost_reference"] == pytest.approx(130.0)


def test_issue_and_finish_stay_on_the_wo_shelf(db):
    r1, asm, cust, shelf = _world(db)
    with db.connect() as conn:
        stock.post_movement(conn, r1, delta=100, mtype=stock.RECEIVE, location_id=shelf)
        conn.commit()
    wo = repo.create_work_order(db, {"assembly_id": asm, "qty": 100, "location_id": shelf})
    repo.issue_work_order(db, wo)
    repo.finish_work_order(db, wo)
    with db.connect() as conn:
        main = stock.default_shelf(conn)
        assert stock.shelf_on_hand(conn, r1, shelf) == 0
        assert stock.shelf_on_hand(conn, r1, main) == 3000
        assert stock.shelf_on_hand(conn, asm, shelf) == 100
        assert stock.shelf_on_hand(conn, asm, main) == 0


def test_work_order_without_shelf_uses_default(db):
    r1, asm, cust, shelf = _world(db)
    wo = repo.get_work_order(db, repo.create_work_order(db, {"assembly_id": asm, "qty": 10}))
    assert wo["shelf"]["kind"] == "ours" and wo["customer_supplied"] is False
    assert wo["lines"][0]["available"] == 3000
    assert wo["loaded_build_cost_charged"] == pytest.approx(13.0)


def test_retired_shelf_refused(db):
    r1, asm, cust, shelf = _world(db)
    with db.connect() as conn:
        conn.execute("UPDATE stock_locations SET retired_at = datetime('now') WHERE id = ?", (shelf,))
        conn.commit()
    with pytest.raises(ValueError, match="shelf"):
        repo.create_work_order(db, {"assembly_id": asm, "qty": 1, "location_id": shelf})


def test_from_order_uses_preferred_shelf(db):
    r1, asm, cust, shelf = _world(db)
    order = corepo.create_order(db, {"customer_id": cust})
    corepo.add_line(db, order, asm, 5, 10.0, None)
    line_id = corepo.get_order(db, order)["lines"][0]["id"]
    assert repo.order_header(db, order)["customer_id"] == cust
    ids = repo.create_work_orders_for_order(
        db, order, {line_id: 5},
        location_id=catrepo.preferred_shelf_id(db, customer_id=cust, order_id=order))
    assert repo.get_work_order(db, ids[0])["location_id"] == shelf
