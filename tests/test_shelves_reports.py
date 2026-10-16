import pytest
from fastapi.testclient import TestClient

import partpilot.web.app as web_app
from partpilot.web.features.catalog import repo as catrepo
from partpilot.web.features.catalog import stock
from partpilot.web.features.contacts import repo as conrepo
from partpilot.web.features.reports import repo as rrepo


@pytest.fixture
def app(tmp_path):
    a = web_app.create_app(db_path=tmp_path / "p.db", data_dir=tmp_path / "data", secret_key="s")
    a.state.store.create_user("boss", "pw", role="admin")
    return a


def test_movement_report_filters_by_shelf(app):
    db = app.state.database
    cust = conrepo.create_contact(db, {"kind": "customer", "name": "Acme"})
    shelf = catrepo.create_shelf(db, kind="customer", owner_contact_id=cust)
    pid = catrepo.create_part(db, part={"part_no": "R-1"}, supplier_lines=[], opening={"qty": 5})
    with db.connect() as conn:
        stock.post_movement(conn, pid, delta=7, mtype=stock.RECEIVE, location_id=shelf)
        conn.commit()
    assert len(rrepo.stock_movements(db)) == 2
    rows = rrepo.stock_movements(db, location_id=shelf)
    assert len(rows) == 1 and rows[0]["location_name"] == "Acme" and rows[0]["location_kind"] == "customer"
    assert rrepo.stock_movement_summary(db, location_id=shelf)["total_in"] == 7
    c = TestClient(app)
    c.post("/login", data={"username": "boss", "password": "pw"})
    html = c.get(f"/reports/stock-movements?location_id={shelf}").text
    assert 'name="location_id"' in html and "Acme" in html
