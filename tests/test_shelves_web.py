import pytest
from fastapi.testclient import TestClient

import partpilot.web.app as web_app
from partpilot.web.features.catalog import repo as catrepo
from partpilot.web.features.contacts import repo as conrepo
from partpilot.web.features.customer_orders import repo as corepo


@pytest.fixture
def app(tmp_path):
    a = web_app.create_app(db_path=tmp_path / "p.db", data_dir=tmp_path / "data", secret_key="s")
    a.state.store.create_user("boss", "pw", role="admin")
    a.state.store.create_user("ship", "pw", role="shipping")
    return a


def _client(app, user="boss"):
    c = TestClient(app)
    c.post("/login", data={"username": user, "password": "pw"})
    return c


def test_create_customer_shelf_from_contact_page(app):
    db = app.state.database
    cust = conrepo.create_contact(db, {"kind": "customer", "name": "Acme"})
    c = _client(app)
    assert "New shelf" in c.get(f"/contacts/{cust}").text
    r = c.post("/catalog/shelves", data={"kind": "customer", "owner_contact_id": cust,
                                          "return_to": f"/contacts/{cust}"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/contacts/{cust}"
    shelves = catrepo.shelves_for_customer(db, cust)
    assert len(shelves) == 1 and shelves[0]["name"] == "Acme"
    assert "Acme" in c.get(f"/contacts/{cust}").text.split("Stock shelves")[1]


def test_create_job_shelf_from_order_page(app):
    db = app.state.database
    cust = conrepo.create_contact(db, {"kind": "customer", "name": "Acme"})
    order = corepo.create_order(db, {"customer_id": cust})
    c = _client(app)
    assert "Create job shelf" in c.get(f"/customer-orders/{order}").text
    c.post("/catalog/shelves", data={"kind": "customer", "owner_contact_id": cust,
                                      "customer_order_id": order,
                                      "return_to": f"/customer-orders/{order}"})
    assert catrepo.shelf_for_order(db, order) is not None
    html = c.get(f"/customer-orders/{order}").text
    assert "Create job shelf" not in html and "Acme · CO-" in html


def test_shelves_list_and_detail_and_retire(app):
    db = app.state.database
    cust = conrepo.create_contact(db, {"kind": "customer", "name": "Acme"})
    sid = catrepo.create_shelf(db, kind="customer", owner_contact_id=cust)
    c = _client(app)
    html = c.get("/catalog/shelves").text
    assert "Main" in html and "Acme" in html and "Stock shelves" in html
    assert "Acme" in c.get(f"/catalog/shelves/{sid}").text
    r = c.post(f"/catalog/shelves/{sid}/retire", follow_redirects=False)
    assert r.status_code == 303
    assert catrepo.get_location(db, sid)["retired_at"]


def test_shelf_create_error_bounces_back_with_message(app):
    db = app.state.database
    cust = conrepo.create_contact(db, {"kind": "customer", "name": "Acme"})
    catrepo.create_shelf(db, kind="customer", owner_contact_id=cust)          # "Acme" exists
    c = _client(app)
    r = c.post("/catalog/shelves", data={"kind": "customer", "owner_contact_id": cust,
                                          "return_to": f"/contacts/{cust}"}, follow_redirects=True)
    assert "already exists" in r.text


def test_bad_return_to_is_ignored_and_shipping_cannot_create(app):
    db = app.state.database
    cust = conrepo.create_contact(db, {"kind": "customer", "name": "Acme"})
    c = _client(app)
    r = c.post("/catalog/shelves", data={"kind": "customer", "owner_contact_id": cust,
                                          "return_to": "https://evil.example/"}, follow_redirects=False)
    assert r.headers["location"].startswith("/catalog/shelves/")
    assert _client(app, "ship").post("/catalog/shelves",
                                     data={"kind": "ours", "name": "X"}).status_code == 403


def test_setup_has_shelves_tile(app):
    assert "/catalog/shelves" in _client(app).get("/setup").text
