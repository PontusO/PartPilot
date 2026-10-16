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
from partpilot.web.features.purchase_orders import feature as po_feature
from partpilot.web.features.purchase_orders import repo
from partpilot.web.features.work_orders import feature as wo_feature
from partpilot.web.features.work_orders import repo as worepo


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "po.db")
    reg = FeatureRegistry()
    reg.register(catalog_feature, assemblies_feature, contacts_feature, co_feature, wo_feature,
                 po_feature)
    database.apply_migrations(reg)
    return database


def _world(db):
    """R-1: 3000 on Main. PROD uses 1×R-1. Acme has a customer shelf (empty)."""
    r1 = catrepo.create_part(db, part={"part_no": "R-1"},
                             supplier_lines=[{"supplier_name": "Acme Supplies", "supplier_pno": "A-1",
                                              "unit_price": 0.5, "reel_qty": 1, "is_default": True}],
                             opening={"qty": 3000})
    asm = asmrepo.create_assembly(db, {"part_no": "PROD"})
    asmrepo.add_bom_line(db, asm, r1, 1, None)
    cust = conrepo.create_contact(db, {"kind": "customer", "name": "Acme"})
    shelf = catrepo.create_shelf(db, kind="customer", owner_contact_id=cust)
    return r1, asm, shelf


def test_shortage_is_per_shelf(db):
    r1, asm, shelf = _world(db)
    worepo.create_work_order(db, {"assembly_id": asm, "qty": 100, "location_id": shelf})
    sugg = repo.shortage_suggestions(db)
    assert len(sugg) == 1
    assert sugg[0]["location_id"] == shelf and sugg[0]["free"] == 0 and sugg[0]["short"] == 100
    assert sugg[0]["location_name"] == "Acme" and sugg[0]["location_kind"] == "customer"
    # An open PO into Main does not cover the customer shelf.
    po = repo.create_po(db, {"supplier_id": sugg[0]["supplier_id"]})
    repo.add_line(db, po, r1, 100, 0.5)
    with db.connect() as conn:
        conn.execute("UPDATE purchase_orders SET status = 'ordered' WHERE id = ?", (po,))
        conn.commit()
    assert repo.shortage_suggestions(db)[0]["short"] == 100


def test_suggestions_grouped_by_shelf_then_supplier_and_create_pos(db):
    r1, asm, shelf = _world(db)
    main = catrepo.default_shelf_id(db)
    worepo.create_work_order(db, {"assembly_id": asm, "qty": 100, "location_id": shelf})
    worepo.create_work_order(db, {"assembly_id": asm, "qty": 4000, "location_id": main})  # 1000 short
    groups = repo.shortage_suggestions_grouped(db)
    assert [g["location_id"] for g in groups] == [main, shelf]
    assert groups[1]["suppliers"][0]["lines"][0]["short"] == 100
    ids = repo.create_pos_from_suggestions(db, {(r1, shelf): 100, (r1, main): 1000})
    assert len(ids) == 2
    by_loc = {repo.get_po(db, i)["location_id"]: repo.get_po(db, i) for i in ids}
    assert by_loc[shelf]["lines"][0]["qty"] == 100 and by_loc[main]["lines"][0]["qty"] == 1000
    assert by_loc[shelf]["location_name"] == "Acme"


def test_receive_lands_on_po_shelf_not_main(db):
    r1, asm, shelf = _world(db)
    po = repo.create_po(db, {"supplier_id": None, "location_id": shelf})
    repo.add_line(db, po, r1, 50, 0.4)
    with db.connect() as conn:
        conn.execute("UPDATE purchase_orders SET status = 'ordered' WHERE id = ?", (po,))
        conn.commit()
    line_id = repo.get_po(db, po)["lines"][0]["id"]
    repo.receive_po(db, po, {line_id: 50})
    with db.connect() as conn:
        assert stock.shelf_on_hand(conn, r1, shelf) == 50
        assert stock.shelf_on_hand(conn, r1, catrepo.default_shelf_id(db)) == 3000
        p = conn.execute("SELECT total_qty, customer_qty FROM parts WHERE id = ?", (r1,)).fetchone()
    assert p["total_qty"] == 3000 and p["customer_qty"] == 50


def test_receive_legacy_po_without_shelf_lands_on_main(db):
    r1, asm, shelf = _world(db)
    po = repo.create_po(db, {"supplier_id": None})
    repo.add_line(db, po, r1, 5, 0.4)
    with db.connect() as conn:
        conn.execute("UPDATE purchase_orders SET status = 'ordered', location_id = NULL WHERE id = ?",
                     (po,))
        conn.commit()
    line_id = repo.get_po(db, po)["lines"][0]["id"]
    repo.receive_po(db, po, {line_id: 5})
    with db.connect() as conn:
        assert stock.shelf_on_hand(conn, r1, catrepo.default_shelf_id(db)) == 3005


def test_receive_into_retired_shelf_refused(db):
    r1, asm, shelf = _world(db)
    po = repo.create_po(db, {"supplier_id": None, "location_id": shelf})
    repo.add_line(db, po, r1, 5, 0.4)
    with db.connect() as conn:
        conn.execute("UPDATE purchase_orders SET status = 'ordered' WHERE id = ?", (po,))
        conn.execute("UPDATE stock_locations SET retired_at = datetime('now') WHERE id = ?", (shelf,))
        conn.commit()
    line_id = repo.get_po(db, po)["lines"][0]["id"]
    with pytest.raises(ValueError, match="retired"):
        repo.receive_po(db, po, {line_id: 5})
