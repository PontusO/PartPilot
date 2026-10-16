import pytest
from fastapi.testclient import TestClient

import partpilot.web.app as web_app
from partpilot.web.features.catalog import repo as catrepo
from partpilot.web.features.catalog import stock
from partpilot.web.features.contacts import repo as conrepo


@pytest.fixture
def app(tmp_path):
    a = web_app.create_app(db_path=tmp_path / "p.db", data_dir=tmp_path / "data", secret_key="s")
    a.state.store.create_user("boss", "pw", role="admin")
    return a


def _c(app):
    c = TestClient(app)
    c.post("/login", data={"username": "boss", "password": "pw"})
    return c


def _two_shelves(db):
    cust = conrepo.create_contact(db, {"kind": "customer", "name": "Acme"})
    shelf = catrepo.create_shelf(db, kind="customer", owner_contact_id=cust)
    pid = catrepo.create_part(db, part={"part_no": "R-1"}, supplier_lines=[], opening={"qty": 100})
    with db.connect() as conn:
        stock.post_movement(conn, pid, delta=25, mtype=stock.RECEIVE, location_id=shelf)
        conn.commit()
    return pid, shelf


def test_part_page_shows_per_shelf_rows_and_totals(app):
    pid, shelf = _two_shelves(app.state.database)
    part = catrepo.get_part(app.state.database, pid)
    assert part["stock_ours"] == 100 and part["stock_customer"] == 25
    kinds = {s["location_name"]: s["kind"] for s in part["stock"]}
    assert kinds == {"Main": "ours", "Acme": "customer"}
    html = _c(app).get(f"/catalog/{pid}").text
    assert "Ours: <b>100</b>" in html and "Customer-owned: <b>25</b>" in html


def test_adjust_targets_selected_shelf(app):
    db = app.state.database
    pid, shelf = _two_shelves(db)
    _c(app).post(f"/catalog/{pid}/stock/move",
                 data={"action": "adjust", "qty": "30", "location_id": shelf})
    with db.connect() as conn:
        assert stock.shelf_on_hand(conn, pid, shelf) == 30
        assert stock.shelf_on_hand(conn, pid, stock.default_shelf(conn)) == 100


def test_part_edit_never_rehomes_customer_row(app):
    db = app.state.database
    pid, shelf = _two_shelves(db)
    catrepo.update_part(db, pid, part={"part_no": "R-1"}, supplier_lines=[],
                        stock={"qty": 120, "location_id": None, "bin": "A1"})
    with db.connect() as conn:
        assert stock.shelf_on_hand(conn, pid, shelf) == 25
        assert stock.shelf_on_hand(conn, pid, stock.default_shelf(conn)) == 120


def test_part_form_offers_only_our_shelves(app):
    db = app.state.database
    pid, shelf = _two_shelves(db)
    html = _c(app).get(f"/catalog/{pid}/edit").text
    stock_block = html.split('name="location_id"')[1].split("</select>")[0]
    assert "Main" in stock_block and "Acme" not in stock_block
