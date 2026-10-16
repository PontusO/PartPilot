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
from partpilot.web.features.despatch import feature as despatch_feature
from partpilot.web.features.despatch import repo as drepo
from partpilot.web.features.work_orders import feature as wo_feature
from partpilot.web.features.work_orders import repo as worepo


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "f.db")
    reg = FeatureRegistry()
    reg.register(catalog_feature, assemblies_feature, contacts_feature, co_feature, wo_feature,
                 despatch_feature)
    database.apply_migrations(reg)
    return database


def _world(db):
    prod = asmrepo.create_assembly(db, {"part_no": "PROD"})
    acme = conrepo.create_contact(db, {"kind": "customer", "name": "Acme"})
    beta = conrepo.create_contact(db, {"kind": "customer", "name": "Beta"})
    a_shelf = catrepo.create_shelf(db, kind="customer", owner_contact_id=acme)
    b_shelf = catrepo.create_shelf(db, kind="customer", owner_contact_id=beta)
    main = catrepo.default_shelf_id(db)
    with db.connect() as conn:
        stock.post_movement(conn, prod, delta=10, mtype=stock.BUILD, location_id=a_shelf)
        stock.post_movement(conn, prod, delta=50, mtype=stock.BUILD, location_id=b_shelf)
        stock.post_movement(conn, prod, delta=3, mtype=stock.BUILD, location_id=main)
        conn.commit()
    return prod, acme, beta, a_shelf, b_shelf, main


def test_proposals_count_only_customer_and_own_shelves(db):
    prod, acme, beta, a_shelf, b_shelf, main = _world(db)
    order = corepo.create_order(db, {"customer_id": acme})
    corepo.add_line(db, order, prod, 20, 10.0, None)
    p = worepo.fulfilment_proposals(db, order)[0]
    assert p["free"] == 13 and p["shortfall"] == 7      # Acme 10 + Main 3; Beta's 50 ignored


def test_allocation_prefers_customer_shelf_then_main_and_despatch_issues_from_them(db):
    prod, acme, beta, a_shelf, b_shelf, main = _world(db)
    order = corepo.create_order(db, {"customer_id": acme, "status": "confirmed"})
    corepo.add_line(db, order, prod, 12, 10.0, None)
    assert corepo.allocate_order(db, order) == 12
    with db.connect() as conn:
        allocs = {r["location_id"]: r["qty"] for r in conn.execute(
            "SELECT location_id, qty FROM allocations")}
        assert allocs == {a_shelf: 10, main: 2}
        assert stock.free_on_shelf(conn, prod, a_shelf) == 0
        assert stock.free_on_shelf(conn, prod, main) == 1
        assert conn.execute("SELECT total_alloc FROM parts WHERE id = ?", (prod,)).fetchone()[0] == 2
    line_id = corepo.get_order(db, order)["lines"][0]["id"]
    d = drepo.create_packing_list(db, order, {line_id: 12}, "u")
    drepo.set_packing(db, d, {ln["id"] for ln in drepo.get_despatch(db, d)["lines"]})
    drepo.confirm_packed(db, d, "u")
    drepo.dispatch(db, d, "u")
    with db.connect() as conn:
        assert stock.shelf_on_hand(conn, prod, a_shelf) == 0
        assert stock.shelf_on_hand(conn, prod, main) == 1
        assert stock.shelf_on_hand(conn, prod, b_shelf) == 50
        assert conn.execute("SELECT COUNT(*) FROM allocations").fetchone()[0] == 0


def test_allocate_without_customer_shelf_uses_ours(db):
    prod, acme, beta, a_shelf, b_shelf, main = _world(db)
    carl = conrepo.create_contact(db, {"kind": "customer", "name": "Carl"})
    order = corepo.create_order(db, {"customer_id": carl})
    corepo.add_line(db, order, prod, 2, 10.0, None)
    assert corepo.allocate_order(db, order) == 2
    with db.connect() as conn:
        assert {r[0] for r in conn.execute("SELECT location_id FROM allocations")} == {main}


def test_shippable_lines_count_candidate_shelves(db):
    prod, acme, beta, a_shelf, b_shelf, main = _world(db)
    order = corepo.create_order(db, {"customer_id": acme})
    corepo.add_line(db, order, prod, 20, 10.0, None)
    ln = drepo.shippable_lines(db, order)[0]
    assert ln["suggested_qty"] == 13                     # Acme 10 + Main 3, never Beta's
