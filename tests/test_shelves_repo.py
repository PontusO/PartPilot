import pytest

from partpilot.web.core import FeatureRegistry
from partpilot.web.core.db import Database
from partpilot.web.features.assemblies import feature as assemblies_feature
from partpilot.web.features.catalog import feature as catalog_feature
from partpilot.web.features.catalog import repo as catrepo
from partpilot.web.features.catalog import stock
from partpilot.web.features.contacts import feature as contacts_feature
from partpilot.web.features.contacts import repo as conrepo
from partpilot.web.features.customer_orders import feature as co_feature
from partpilot.web.features.customer_orders import repo as corepo
from partpilot.web.features.purchase_orders import feature as po_feature
from partpilot.web.features.purchase_orders import repo as porepo
from partpilot.web.features.work_orders import feature as wo_feature


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "shelves.db")
    reg = FeatureRegistry()
    reg.register(catalog_feature, assemblies_feature, contacts_feature, co_feature, wo_feature,
                 po_feature)
    database.apply_migrations(reg)
    return database


def _customer(db, name="Acme"):
    return conrepo.create_contact(db, {"kind": "customer", "name": name, "short_name": name})


def _order(db, cust):
    return corepo.create_order(db, {"customer_id": cust})


def test_create_customer_and_job_shelves_autonamed(db):
    cust = _customer(db)
    order = _order(db, cust)
    s1 = catrepo.create_shelf(db, kind="customer", owner_contact_id=cust)
    s2 = catrepo.create_shelf(db, kind="customer", owner_contact_id=cust, customer_order_id=order)
    rows = {r["id"]: r for r in catrepo.locations(db)}
    assert rows[s1]["name"] == "Acme" and rows[s1]["kind"] == "customer"
    assert rows[s1]["owner_name"] == "Acme"
    assert rows[s2]["name"].startswith("Acme · CO-")
    assert rows[s2]["order_ref"] == rows[s2]["name"].split(" · ")[1]


def test_create_shelf_validation(db):
    sup = conrepo.create_contact(db, {"kind": "supplier", "name": "Digi"})
    with pytest.raises(ValueError):
        catrepo.create_shelf(db, kind="customer")                       # no owner
    with pytest.raises(ValueError):
        catrepo.create_shelf(db, kind="customer", owner_contact_id=sup)  # not a customer
    catrepo.default_shelf_id(db)                                         # make sure "Main" exists
    with pytest.raises(ValueError):
        catrepo.create_shelf(db, kind="ours", name="Main")               # duplicate name
    with pytest.raises(ValueError):
        catrepo.create_shelf(db, kind="elsewhere", name="X")


def test_pickable_and_preferred(db):
    acme, beta = _customer(db, "Acme"), _customer(db, "Beta")
    order = _order(db, acme)
    a_shelf = catrepo.create_shelf(db, kind="customer", owner_contact_id=acme)
    job = catrepo.create_shelf(db, kind="customer", owner_contact_id=acme, customer_order_id=order)
    catrepo.create_shelf(db, kind="customer", owner_contact_id=beta)
    main = catrepo.default_shelf_id(db)
    ids = [s["id"] for s in catrepo.pickable_shelves(db, customer_id=acme)]
    assert ids == [main, job, a_shelf]                      # ours first, then job, then customer
    assert catrepo.preferred_shelf_id(db, customer_id=acme, order_id=order) == job
    assert catrepo.preferred_shelf_id(db, customer_id=acme) == a_shelf
    assert catrepo.preferred_shelf_id(db, customer_id=beta, order_id=order) != job
    assert catrepo.preferred_shelf_id(db) == main
    assert catrepo.candidate_shelf_ids(db, customer_id=acme, order_id=order) == [job, a_shelf, main]


def test_retire_rules(db):
    cust = _customer(db)
    shelf = catrepo.create_shelf(db, kind="customer", owner_contact_id=cust)
    pid = catrepo.create_part(db, part={"part_no": "R-1"}, supplier_lines=[], opening=None)
    main = catrepo.default_shelf_id(db)
    with pytest.raises(ValueError):
        catrepo.retire_shelf(db, main)                       # last own shelf
    with db.connect() as conn:
        stock.post_movement(conn, pid, delta=5, mtype=stock.RECEIVE, location_id=shelf)
        conn.commit()
    with pytest.raises(ValueError, match="stock"):
        catrepo.retire_shelf(db, shelf)
    with db.connect() as conn:
        stock.post_movement(conn, pid, delta=-5, mtype=stock.ADJUST, location_id=shelf)
        conn.commit()
    po = porepo.create_po(db, {"supplier_id": None, "location_id": shelf})
    with pytest.raises(ValueError, match="purchase order"):
        catrepo.retire_shelf(db, shelf)
    with db.connect() as conn:
        conn.execute("UPDATE purchase_orders SET status = 'cancelled' WHERE id = ?", (po,))
        conn.commit()
    catrepo.retire_shelf(db, shelf)
    assert shelf not in [s["id"] for s in catrepo.pickable_shelves(db, customer_id=cust)]
    assert shelf in [s["id"] for s in catrepo.locations(db, include_retired=True)]


def test_shelf_stock_and_value(db):
    cust = _customer(db)
    shelf = catrepo.create_shelf(db, kind="customer", owner_contact_id=cust)
    pid = catrepo.create_part(db, part={"part_no": "R-1"},
                              supplier_lines=[{"supplier_name": "X", "unit_price": 2.0, "reel_qty": 1,
                                               "is_default": True}], opening=None)
    with db.connect() as conn:
        stock.post_movement(conn, pid, delta=10, mtype=stock.RECEIVE, location_id=shelf)
        conn.commit()
    rows = catrepo.shelf_stock(db, shelf)
    assert rows[0]["part_no"] == "R-1" and rows[0]["on_hand"] == 10 and rows[0]["free"] == 10
    assert catrepo.get_location(db, shelf)["value_total"] == pytest.approx(20.0)
    assert catrepo.shelf_movements(db, shelf)[0]["qty_delta"] == 10
