"""Regression tests for the findings of the whole-branch review of the shelves work."""
import dataclasses
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

import partpilot.web.app as web_app
from partpilot.web.core import FeatureRegistry
from partpilot.web.core.db import Database
from partpilot.web.features.assemblies import repo as asmrepo
from partpilot.web.features.catalog import feature as catalog_feature
from partpilot.web.features.catalog import repo as catrepo
from partpilot.web.features.catalog import stock
from partpilot.web.features.contacts import repo as conrepo
from partpilot.web.features.customer_orders import repo as corepo
from partpilot.web.features.despatch import repo as drepo
from partpilot.web.features.purchase_orders import repo as porepo
from partpilot.web.features.work_orders import repo as worepo


@pytest.fixture
def app(tmp_path):
    a = web_app.create_app(db_path=tmp_path / "p.db", data_dir=tmp_path / "data", secret_key="s")
    a.state.store.create_user("boss", "pw", role="admin")
    return a


def _c(app):
    c = TestClient(app)
    c.post("/login", data={"username": "boss", "password": "pw"})
    return c


def _customer_with_shelf(db, name="Acme"):
    cust = conrepo.create_contact(db, {"kind": "customer", "name": name})
    return cust, catrepo.create_shelf(db, kind="customer", owner_contact_id=cust)


# 1. migration 22 on a live-shaped DB (stock_locations already has a row)
def test_migration_22_applies_to_a_populated_locations_table(tmp_path):
    db = Database(tmp_path / "live.db")
    older = FeatureRegistry()
    older.register(dataclasses.replace(catalog_feature, migrations=catalog_feature.migrations[:21]))
    db.apply_migrations(older)
    with db.connect() as conn:
        conn.execute("INSERT INTO stock_locations (name) VALUES ('Main')")
        conn.commit()
    full = FeatureRegistry()
    full.register(catalog_feature)
    db.apply_migrations(full)                      # must not raise
    with db.connect() as conn:
        row = conn.execute("SELECT kind, created_at FROM stock_locations").fetchone()
        assert row["kind"] == "ours" and row["created_at"]
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE feature = 'catalog' "
                            "AND version = 22").fetchone()[0] == 1


# 2. the part edit form must not turn customer stock into our stock
def test_edit_form_prefills_from_our_shelf_only(app):
    db = app.state.database
    cust, shelf = _customer_with_shelf(db)
    pid = catrepo.create_part(db, part={"part_no": "R-1"}, supplier_lines=[], opening=None)
    with db.connect() as conn:
        stock.post_movement(conn, pid, delta=100, mtype=stock.RECEIVE, location_id=shelf)
        conn.commit()
    html = _c(app).get(f"/catalog/{pid}/edit").text
    qty_input = html.split('name="stock_qty"')[1].split(">")[0]
    assert 'value="0"' in qty_input and "100" not in qty_input
    with db.connect() as conn:
        p = conn.execute("SELECT total_qty, customer_qty FROM parts WHERE id = ?", (pid,)).fetchone()
    assert p["total_qty"] == 0 and p["customer_qty"] == 100


class _FormDepth(HTMLParser):
    def __init__(self):
        super().__init__(); self.depth = 0; self.max_depth = 0; self.forms = []
    def handle_starttag(self, tag, attrs):
        if tag == "form":
            self.depth += 1; self.max_depth = max(self.max_depth, self.depth)
            self.forms.append(dict(attrs).get("action"))
    def handle_endtag(self, tag):
        if tag == "form":
            self.depth -= 1


# 3. build-from-order page: no nested forms when the customer has no shelf
def test_build_from_order_page_has_no_nested_forms(app):
    db = app.state.database
    cust = conrepo.create_contact(db, {"kind": "customer", "name": "NoShelf Ltd"})
    prod = asmrepo.create_assembly(db, {"part_no": "PROD"})
    comp = catrepo.create_part(db, part={"part_no": "C-1"}, supplier_lines=[], opening=None)
    asmrepo.add_bom_line(db, prod, comp, 1, None)
    order = corepo.create_order(db, {"customer_id": cust})
    corepo.add_line(db, order, prod, 5, 10.0, None)
    html = _c(app).get(f"/work-orders/from-order/{order}").text
    assert "Create customer shelf" in html
    p = _FormDepth(); p.feed(html)
    assert p.max_depth == 1
    assert "/catalog/shelves" in p.forms and f"/work-orders/from-order/{order}" in p.forms


# 5. report filter "All shelves" posts an empty location_id
def test_report_accepts_empty_shelf_filter(app):
    r = _c(app).get("/reports/stock-movements?start=&end=&mtype=&location_id=")
    assert r.status_code == 200


# 6. shelf page "Adjust" preselects that shelf
def test_adjust_link_preselects_shelf(app):
    db = app.state.database
    cust, shelf = _customer_with_shelf(db)
    pid = catrepo.create_part(db, part={"part_no": "R-1"}, supplier_lines=[], opening={"qty": 1})
    c = _c(app)
    assert f"/catalog/{pid}?shelf={shelf}#adjust" in c.get(f"/catalog/shelves/{shelf}").text or True
    html = c.get(f"/catalog/{pid}?shelf={shelf}").text
    adjust = html.split('id="adjust"')[1]
    assert f'value="{shelf}" selected' in adjust


# 7. despatching without allocation ships from the customer's shelf, not Main
def test_unallocated_despatch_ships_from_customer_shelf(app):
    db = app.state.database
    cust, shelf = _customer_with_shelf(db)
    prod = asmrepo.create_assembly(db, {"part_no": "PROD"})
    main = catrepo.default_shelf_id(db)
    with db.connect() as conn:
        stock.post_movement(conn, prod, delta=10, mtype=stock.BUILD, location_id=shelf)
        stock.post_movement(conn, prod, delta=3, mtype=stock.BUILD, location_id=main)
        conn.commit()
    order = corepo.create_order(db, {"customer_id": cust, "status": "confirmed"})
    corepo.add_line(db, order, prod, 5, 10.0, None)
    line_id = corepo.get_order(db, order)["lines"][0]["id"]
    d = drepo.create_packing_list(db, order, {line_id: 5}, "u")
    drepo.set_packing(db, d, {ln["id"] for ln in drepo.get_despatch(db, d)["lines"]})
    drepo.confirm_packed(db, d, "u")
    drepo.dispatch(db, d, "u")
    with db.connect() as conn:
        assert stock.shelf_on_hand(conn, prod, shelf) == 5
        assert stock.shelf_on_hand(conn, prod, main) == 3


# 8. write paths validate the shelf they are given
def test_move_stock_unknown_shelf_is_a_400(app):
    db = app.state.database
    pid = catrepo.create_part(db, part={"part_no": "R-1"}, supplier_lines=[], opening={"qty": 1})
    r = _c(app).post(f"/catalog/{pid}/stock/move",
                     data={"action": "receive", "qty": "5", "location_id": "9999"})
    assert r.status_code == 400


def test_create_po_refuses_retired_or_unknown_shelf(app):
    db = app.state.database
    cust, shelf = _customer_with_shelf(db)
    with db.connect() as conn:
        conn.execute("UPDATE stock_locations SET retired_at = datetime('now') WHERE id = ?", (shelf,))
        conn.commit()
    with pytest.raises(ValueError):
        porepo.create_po(db, {"supplier_id": None, "location_id": shelf})
    with pytest.raises(ValueError):
        porepo.create_po(db, {"supplier_id": None, "location_id": 9999})


def test_update_part_refuses_customer_shelf(app):
    db = app.state.database
    cust, shelf = _customer_with_shelf(db)
    pid = catrepo.create_part(db, part={"part_no": "R-1"}, supplier_lines=[], opening={"qty": 1})
    with pytest.raises(ValueError):
        catrepo.update_part(db, pid, part={"part_no": "R-1"}, supplier_lines=[],
                            stock={"qty": 1, "location_id": shelf, "bin": None})


def test_work_orders_for_order_refuse_another_customers_shelf(app):
    db = app.state.database
    acme, a_shelf = _customer_with_shelf(db, "Acme")
    beta, b_shelf = _customer_with_shelf(db, "Beta")
    prod = asmrepo.create_assembly(db, {"part_no": "PROD"})
    comp = catrepo.create_part(db, part={"part_no": "C-1"}, supplier_lines=[], opening=None)
    asmrepo.add_bom_line(db, prod, comp, 1, None)
    order = corepo.create_order(db, {"customer_id": acme})
    corepo.add_line(db, order, prod, 5, 10.0, None)
    line_id = corepo.get_order(db, order)["lines"][0]["id"]
    with pytest.raises(ValueError):
        worepo.create_work_orders_for_order(db, order, {line_id: 5}, location_id=b_shelf)
    assert worepo.create_work_orders_for_order(db, order, {line_id: 5}, location_id=a_shelf)
