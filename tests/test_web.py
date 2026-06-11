from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import digisearch.web.app as web_app
import digisearch.web.features.purchasing.router as purchasing_router
from digisearch.models import BomLine, Candidate, LineKind, ResolvedLine, Status
from digisearch.web.features.purchasing.service import PurchaseResult


@pytest.fixture
def app(tmp_path):
    application = web_app.create_app(
        db_path=tmp_path / "partpilot.db",
        data_dir=tmp_path / "data",
        secret_key="test-secret",
    )
    store = application.state.store
    store.create_user("buyer1", "pw", role="purchasing")
    store.create_user("ware1", "pw", role="warehouse")
    return application


def _login(client: TestClient, username: str, password: str):
    return client.post(
        "/login", data={"username": username, "password": password}, follow_redirects=False
    )


def _fake_run_purchase(bom_path, out_dir, *, build_qty=1, check_stock=True, **kw):
    out_dir = Path(out_dir)
    report = out_dir / f"{Path(bom_path).stem}-resolved.xlsx"
    report.write_bytes(b"fake-xlsx")
    cart = out_dir / f"{Path(bom_path).stem}-resolved-digikey-cart.csv"
    cart.write_text("Quantity,Digi-Key Part Number\n10,ABC-ND\n")
    cand = Candidate(supplier="Digi-Key", mpn="MPN1", dk_part_number="ABC-ND")
    line = ResolvedLine(
        line=BomLine(refdes=["R1"], qty=1, value="10k"), kind=LineKind.MPN, chosen=cand,
        status=Status.RESOLVED, confidence=0.91, packaging="Cut tape",
        purchase_qty=build_qty, purchase_unit_price=0.01, line_cost=0.1,
    )
    return PurchaseResult(
        resolved=[line], report_path=report, cart_paths={"Digi-Key": cart},
        summary={"resolved": 1}, build_qty=build_qty, currency="SEK", total_cost=0.1,
        stock_checked=check_stock, mouser_enabled=False,
    )


def test_index_requires_login(app):
    client = TestClient(app)
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_bad_login_rejected(app):
    client = TestClient(app)
    r = _login(client, "buyer1", "nope")
    assert r.status_code == 401


def test_home_shows_welcome_with_categories(app):
    client = TestClient(app)
    _login(client, "buyer1", "pw")
    r = client.get("/")
    assert r.status_code == 200 and "PartPilot" in r.text
    # The category nav (built features + placeholders) is rendered in the sidebar.
    for label in ("Parts", "Assemblies", "Work Orders", "Customer Orders",
                  "Purchasing", "Contacts", "Reports"):
        assert label in r.text, label
    assert "soon" in r.text  # placeholders tagged


def test_contacts_list_create_and_edit(app):
    from digisearch.web.features.contacts import repo as crepo

    client = TestClient(app)
    _login(client, "buyer1", "pw")  # purchasing -> can edit
    assert "New Contact" in client.get("/contacts").text
    assert client.get("/contacts/new").status_code == 200

    r = client.post("/contacts/new",
                    data={"kind": "customer", "name": "Acme Corp", "email": "a@acme.com",
                          "contact": "Jane"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/contacts"
    lst = client.get("/contacts?kind=customer").text
    assert "Acme Corp" in lst and "customer" in lst

    cid = crepo.list_contacts(app.state.database, search="Acme")[0]["id"]
    f = client.get(f"/contacts/{cid}/edit")
    assert f.status_code == 200 and 'value="Acme Corp"' in f.text
    e = client.post(f"/contacts/{cid}/edit",
                    data={"kind": "customer", "name": "Acme Inc"}, follow_redirects=False)
    assert e.status_code == 303
    assert "Acme Inc" in client.get("/contacts").text


def test_planning_calendar_events_and_reschedule(app):
    from digisearch.web.features.assemblies import repo as asmrepo
    from digisearch.web.features.catalog import repo as catrepo
    from digisearch.web.features.contacts import repo as conrepo
    from digisearch.web.features.customer_orders import repo as corepo
    from digisearch.web.features.work_orders import repo as worepo

    db = app.state.database
    comp = catrepo.create_part(db, part={"part_no": "PLC-1"},
                               supplier_lines=[{"supplier_name": "X", "unit_price": 1,
                                                "reel_qty": 1, "is_default": True}])
    asm = asmrepo.create_assembly(db, {"part_no": "PLA-1", "default_build_days": 3})
    asmrepo.add_bom_line(db, asm, comp, 1, None)
    cust = conrepo.create_contact(db, {"kind": "customer", "name": "Acme"})
    oid = corepo.create_order(db, {"customer_id": cust, "required_date": "2026-08-31"})
    corepo.add_line(db, oid, asm, 2, 50.0, None)
    line_id = corepo.get_order(db, oid)["lines"][0]["id"]
    wo_id = worepo.create_work_orders_for_order(db, oid, {line_id: 2}, "buyer1")[0]

    client = TestClient(app)
    _login(client, "buyer1", "pw")
    page = client.get("/planning").text
    assert "FullCalendar.Calendar" in page and "/static/fullcalendar/index.global.min.js" in page
    assert client.get("/static/fullcalendar/index.global.min.js").status_code == 200  # vendored asset served

    events = client.get("/planning/events").json()
    assert any(e["extendedProps"]["type"] == "wo" for e in events)

    r = client.post("/planning/reschedule", data={"wo_id": str(wo_id), "start": "2026-09-01"},
                    follow_redirects=False)
    assert r.status_code == 204
    assert worepo.get_work_order(db, wo_id)["planned_start"] == "2026-09-01"

    # dragging the purchasing marker moves only the purchasing date
    before_start = worepo.get_work_order(db, wo_id)["planned_start"]
    rp = client.post("/planning/reschedule", data={"wo_id": str(wo_id), "purchase": "2026-08-10"},
                     follow_redirects=False)
    assert rp.status_code == 204
    wo = worepo.get_work_order(db, wo_id)
    assert wo["purchase_by"] == "2026-08-10" and wo["planned_start"] == before_start


def test_planning_reschedule_requires_role(app):
    app.state.store.create_user("ship9", "pw", role="shipping")  # not a planning role
    client = TestClient(app)
    _login(client, "ship9", "pw")
    assert client.get("/planning").status_code == 200  # viewing is fine
    r = client.post("/planning/reschedule", data={"wo_id": "1", "start": "2026-09-01"},
                    follow_redirects=False)
    assert r.status_code == 403


def test_work_order_flow(app):
    from digisearch.web.features.assemblies import repo as asmrepo
    from digisearch.web.features.catalog import repo as catrepo
    from digisearch.web.features.work_orders import repo as worepo

    db = app.state.database
    comp = catrepo.create_part(db, part={"part_no": "WC-1"},
                               supplier_lines=[{"supplier_name": "X", "unit_price": 1,
                                                "reel_qty": 1, "is_default": True}],
                               opening={"qty": 100})
    asm = asmrepo.create_assembly(db, {"part_no": "WA-1"})
    asmrepo.add_bom_line(db, asm, comp, 4, None)

    client = TestClient(app)
    _login(client, "buyer1", "pw")
    r = client.post("/work-orders/new", data={"assembly_id": asm, "qty": "5", "wo_no": "WO-77"},
                    follow_redirects=False)
    assert r.status_code == 303
    wid = int(r.headers["location"].rsplit("/", 1)[1])

    page = client.get(f"/work-orders/{wid}").text
    assert "WO-77" in page and "WC-1" in page and "allocated" in page

    client.post(f"/work-orders/{wid}/flush", follow_redirects=False)  # issue + finish
    assert worepo.get_work_order(db, wid)["status"] == "finished"
    assert catrepo.get_part(db, comp)["total_qty"] == 80  # 100 − 4×5
    assert catrepo.get_part(db, asm)["total_qty"] == 5    # built into stock
    assert "WO-77" in client.get("/work-orders").text


def test_build_to_fulfil_from_customer_order(app):
    from digisearch.web.features.assemblies import repo as asmrepo
    from digisearch.web.features.catalog import repo as catrepo
    from digisearch.web.features.contacts import repo as conrepo
    from digisearch.web.features.customer_orders import repo as corepo
    from digisearch.web.features.work_orders import repo as worepo

    db = app.state.database
    comp = catrepo.create_part(db, part={"part_no": "BC-1"},
                               supplier_lines=[{"supplier_name": "X", "unit_price": 1,
                                                "reel_qty": 1, "is_default": True}],
                               opening={"qty": 100})
    asm = asmrepo.create_assembly(db, {"part_no": "BA-1"})
    asmrepo.add_bom_line(db, asm, comp, 2, None)
    cust = conrepo.create_contact(db, {"kind": "customer", "name": "Acme"})
    oid = corepo.create_order(db, {"customer_id": cust, "order_ref": "SO-5"})
    corepo.add_line(db, oid, asm, 7, 10.0, None)   # 7× BA-1, none in stock -> short

    client = TestClient(app)
    _login(client, "buyer1", "pw")
    page = client.get(f"/customer-orders/{oid}").text
    assert "can't be filled from stock" in page and f"/work-orders/from-order/{oid}" in page

    review = client.get(f"/work-orders/from-order/{oid}").text
    assert "BA-1" in review and "Build to fulfil" in review

    line_id = next(p["line_id"] for p in worepo.fulfilment_proposals(db, oid) if p["part_no"] == "BA-1")
    r = client.post(f"/work-orders/from-order/{oid}",
                    data={"build": str(line_id), f"qty_{line_id}": "7"}, follow_redirects=False)
    assert r.status_code == 303

    wo = worepo.work_orders_for_order(db, oid)[line_id][0]
    assert wo["qty"] == 7 and wo["wo_no"] == f"WO-{wo['id']:05d}"  # zero-padded 5-digit ref

    page2 = client.get(f"/customer-orders/{oid}").text
    assert wo["wo_no"] in page2 and "can't be filled from stock" not in page2  # covered now


def test_work_order_write_requires_role(app):
    app.state.store.create_user("ship3", "pw", role="shipping")  # not a work-order role
    client = TestClient(app)
    _login(client, "ship3", "pw")
    assert client.get("/work-orders").status_code == 200
    assert client.get("/work-orders/new").status_code == 403


def test_purchase_order_suggestions_and_receive(app):
    from digisearch.web.features.assemblies import repo as asmrepo
    from digisearch.web.features.catalog import repo as catrepo
    from digisearch.web.features.purchase_orders import repo as porepo
    from digisearch.web.features.work_orders import repo as worepo

    db = app.state.database
    p = catrepo.create_part(db, part={"part_no": "PR-1"},
                            supplier_lines=[{"supplier_name": "VendorX", "supplier_pno": "VX-1",
                                             "unit_price": 1.0, "reel_qty": 1, "is_default": True}],
                            opening={"qty": 5})
    asm = asmrepo.create_assembly(db, {"part_no": "PA-1"})
    asmrepo.add_bom_line(db, asm, p, 1, None)
    worepo.create_work_order(db, {"assembly_id": asm, "qty": 20})  # need 20, have 5 → short 15

    client = TestClient(app)
    _login(client, "buyer1", "pw")
    assert "are short" in client.get("/purchase-orders").text  # shortage banner

    sug = client.get("/purchase-orders/suggestions").text
    assert "PR-1" in sug and "VendorX" in sug

    r = client.post("/purchase-orders/suggestions", data={"buy": str(p), f"qty_{p}": "15"},
                    follow_redirects=False)
    assert r.status_code == 303
    po_id = porepo.list_pos(db, status="draft")[0]["id"]

    client.post(f"/purchase-orders/{po_id}/order", follow_redirects=False)
    line_id = porepo.get_po(db, po_id)["lines"][0]["id"]
    rr = client.post(f"/purchase-orders/{po_id}/receive",
                     data={f"recv_{line_id}": "15", "advice_no": "ADV-1"}, follow_redirects=False)
    assert catrepo.get_part(db, p)["total_qty"] == 20  # 5 + 15 received into stock
    assert porepo.get_po(db, po_id)["status"] == "received"
    # receiving raised a Goods Received Note
    assert "/goods-receipts/" in rr.headers["location"]
    assert "GRN-" in client.get("/goods-receipts").text


def test_po_export_downloads(app):
    from digisearch.web.features.catalog import repo as catrepo
    from digisearch.web.features.purchase_orders import repo as porepo

    db = app.state.database
    p = catrepo.create_part(db, part={"part_no": "WEXP-1", "mfr_pno": "M1"},
                            supplier_lines=[{"supplier_name": "Vend", "supplier_pno": "V-1",
                                             "unit_price": 2.0, "reel_qty": 1, "is_default": True}])
    sup_id = porepo.suppliers(db)[0]["id"]
    po_id = porepo.create_po(db, {"supplier_id": sup_id})
    porepo.add_line(db, po_id, p, 50, None)

    client = TestClient(app)
    _login(client, "buyer1", "pw")
    page = client.get(f"/purchase-orders/{po_id}").text
    assert f"/purchase-orders/{po_id}/export.csv" in page and f"/purchase-orders/{po_id}/export.pdf" in page

    rc = client.get(f"/purchase-orders/{po_id}/export.csv")
    assert rc.status_code == 200 and "text/csv" in rc.headers["content-type"]
    assert "attachment" in rc.headers["content-disposition"] and "V-1" in rc.text

    rp = client.get(f"/purchase-orders/{po_id}/export.pdf")
    assert rp.status_code == 200 and rp.headers["content-type"] == "application/pdf"
    assert rp.content[:5] == b"%PDF-"


def test_po_documents_archived_on_placement(app):
    from digisearch.web.features.catalog import repo as catrepo
    from digisearch.web.features.purchase_orders import repo as porepo

    db = app.state.database
    p = catrepo.create_part(db, part={"part_no": "ARC-1"},
                            supplier_lines=[{"supplier_name": "V", "supplier_pno": "V1",
                                             "unit_price": 1.0, "reel_qty": 1, "is_default": True}])
    sup_id = porepo.suppliers(db)[0]["id"]
    po_id = porepo.create_po(db, {"supplier_id": sup_id})
    porepo.add_line(db, po_id, p, 10, None)

    client = TestClient(app)
    _login(client, "buyer1", "pw")
    client.post(f"/purchase-orders/{po_id}/order", follow_redirects=False)  # place → archives docs

    assert len(porepo.documents_for_po(db, po_id)) == 2
    assert "Archived for ISO records" in client.get(f"/purchase-orders/{po_id}").text
    r = client.get(f"/purchase-orders/{po_id}/export.pdf")
    assert r.status_code == 200 and r.content[:5] == b"%PDF-"  # serves the stored copy


def test_production_spillage_setting_inflates_work_order(app):
    from digisearch.web.features.assemblies import repo as asmrepo
    from digisearch.web.features.catalog import repo as catrepo
    from digisearch.web.features.setup import repo as setuprepo
    from digisearch.web.features.work_orders import repo as worepo

    db = app.state.database
    app.state.store.create_user("boss", "pw", role="admin")

    client = TestClient(app)
    _login(client, "buyer1", "pw")                       # purchasing — not admin
    assert client.get("/setup/production", follow_redirects=False).status_code == 403

    _login(client, "boss", "pw")
    assert "Saved" in client.post("/setup/production",
                                  data={"spillage_percent": "5", "min_margin_qty": "15"}).text
    prod = setuprepo.get_production(db)
    assert prod["spillage_percent"] == "5" and prod["min_margin_qty"] == "15"

    comp = catrepo.create_part(db, part={"part_no": "SP-1"},
                               supplier_lines=[{"supplier_name": "X", "unit_price": 1,
                                                "reel_qty": 1, "is_default": True}])
    asm = asmrepo.create_assembly(db, {"part_no": "SPA-1"})
    asmrepo.add_bom_line(db, asm, comp, 1, None)
    # build 100 (base 100): 5% = 5, but the 15 minimum wins → 115
    wo = worepo.get_work_order(db, worepo.create_work_order(db, {"assembly_id": asm, "qty": 100}))
    assert wo["spillage_percent"] == 5.0 and wo["min_margin_qty"] == 15.0
    assert wo["lines"][0]["qty_required"] == 115


def test_company_details_settings(app):
    from digisearch.web.features.setup import repo as setuprepo

    app.state.store.create_user("boss", "pw", role="admin")
    client = TestClient(app)

    # non-admin can't reach the company settings
    _login(client, "buyer1", "pw")
    assert client.get("/setup/company", follow_redirects=False).status_code == 403

    _login(client, "boss", "pw")
    r = client.post("/setup/company",
                    data={"name": "Invector Labs AB", "city": "Gothenburg", "vat_no": "SE123"})
    assert r.status_code == 200 and "Saved" in r.text
    assert setuprepo.get_company(app.state.database)["name"] == "Invector Labs AB"


def test_edit_po_line_before_placing(app):
    from digisearch.web.features.catalog import repo as catrepo
    from digisearch.web.features.purchase_orders import repo as porepo

    db = app.state.database
    p = catrepo.create_part(db, part={"part_no": "EQ-1"},
                            supplier_lines=[{"supplier_name": "S", "unit_price": 1.0,
                                             "reel_qty": 1, "is_default": True}])
    sup_id = porepo.suppliers(db)[0]["id"]
    po_id = porepo.create_po(db, {"supplier_id": sup_id})
    porepo.add_line(db, po_id, p, 10, None)
    line_id = porepo.get_po(db, po_id)["lines"][0]["id"]

    client = TestClient(app)
    _login(client, "buyer1", "pw")
    assert f"/purchase-orders/{po_id}/lines/{line_id}/update" in client.get(f"/purchase-orders/{po_id}").text

    client.post(f"/purchase-orders/{po_id}/lines/{line_id}/update",
                data={"qty": "175", "unit_price": "0.5"}, follow_redirects=False)
    ln = porepo.get_po(db, po_id)["lines"][0]
    assert ln["qty"] == 175 and ln["unit_price"] == 0.5  # both editable inline

    # once placed, the inputs are gone and the route refuses
    porepo.mark_ordered(db, po_id, "buyer1")
    assert f"/lines/{line_id}/update" not in client.get(f"/purchase-orders/{po_id}").text
    assert client.post(f"/purchase-orders/{po_id}/lines/{line_id}/update",
                       data={"qty": "5", "unit_price": "9"}, follow_redirects=False).status_code == 400
    assert porepo.get_po(db, po_id)["lines"][0]["qty"] == 175


def test_delete_po_from_detail_page(app):
    from digisearch.web.features.assemblies import repo as asmrepo
    from digisearch.web.features.catalog import repo as catrepo
    from digisearch.web.features.purchase_orders import repo as porepo
    from digisearch.web.features.work_orders import repo as worepo

    db = app.state.database
    client = TestClient(app)
    _login(client, "buyer1", "pw")

    p = catrepo.create_part(db, part={"part_no": "WD-1"},
                            supplier_lines=[{"supplier_name": "S", "unit_price": 1.0,
                                             "reel_qty": 1, "is_default": True}])
    sup_id = porepo.suppliers(db)[0]["id"]
    po_id = porepo.create_po(db, {"supplier_id": sup_id})
    porepo.add_line(db, po_id, p, 5, None)
    porepo.mark_ordered(db, po_id, "buyer1")  # placed → archived docs, nothing received

    # the list has no delete button; the detail page does
    assert "/delete" not in client.get("/purchase-orders").text
    assert f"/purchase-orders/{po_id}/delete" in client.get(f"/purchase-orders/{po_id}").text

    r = client.post(f"/purchase-orders/{po_id}/delete", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/purchase-orders"
    assert porepo.get_po(db, po_id) is None and porepo.documents_for_po(db, po_id) == []

    # a PO with received goods can't be deleted (no button, route refuses)
    comp = catrepo.create_part(db, part={"part_no": "RC-1"},
                               supplier_lines=[{"supplier_name": "S", "unit_price": 1.0,
                                                "reel_qty": 1, "is_default": True}], opening={"qty": 0})
    asm = asmrepo.create_assembly(db, {"part_no": "RA-1"})
    asmrepo.add_bom_line(db, asm, comp, 1, None)
    worepo.create_work_order(db, {"assembly_id": asm, "qty": 10})  # drives a shortage
    po2 = porepo.create_pos_from_suggestions(db, {comp: 10}, "buyer1")[0]
    porepo.mark_ordered(db, po2, "buyer1")
    line_id = porepo.get_po(db, po2)["lines"][0]["id"]
    porepo.receive_po(db, po2, {line_id: 10}, "buyer1")  # goods in → GRN
    assert f"/purchase-orders/{po2}/delete" not in client.get(f"/purchase-orders/{po2}").text
    assert client.post(f"/purchase-orders/{po2}/delete", follow_redirects=False).status_code == 400
    assert porepo.get_po(db, po2) is not None


def test_purchase_order_write_requires_role(app):
    app.state.store.create_user("ship4", "pw", role="shipping")
    client = TestClient(app)
    _login(client, "ship4", "pw")
    assert client.get("/purchase-orders").status_code == 200
    assert client.get("/purchase-orders/new").status_code == 403


def test_stock_move_via_part_page(app):
    from digisearch.web.features.catalog import repo as catrepo

    db = app.state.database
    pid = catrepo.create_part(db, part={"part_no": "WPART"},
                              supplier_lines=[{"supplier_name": "X", "unit_price": 1.0,
                                               "reel_qty": 1, "is_default": True}],
                              opening={"qty": 100})
    client = TestClient(app)
    _login(client, "ware1", "pw")  # warehouse may move stock

    r = client.post(f"/catalog/{pid}/stock/move",
                    data={"action": "receive", "qty": "25", "note": "delivery"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert catrepo.get_part(db, pid)["total_qty"] == 125

    client.post(f"/catalog/{pid}/stock/move", data={"action": "adjust", "qty": "200"},
                follow_redirects=False)  # set on-hand to 200
    assert catrepo.get_part(db, pid)["total_qty"] == 200

    page = client.get(f"/catalog/{pid}").text
    assert "Stock movements" in page and "RECEIVE" in page and "delivery" in page


def test_stock_move_requires_role(app):
    from digisearch.web.features.catalog import repo as catrepo

    app.state.store.create_user("ship1", "pw", role="shipping")  # not a stock-move role
    pid = catrepo.create_part(app.state.database, part={"part_no": "WPART2"},
                              supplier_lines=[{"supplier_name": "X", "unit_price": 1.0,
                                               "reel_qty": 1, "is_default": True}])
    client = TestClient(app)
    _login(client, "ship1", "pw")
    r = client.post(f"/catalog/{pid}/stock/move", data={"action": "receive", "qty": "5"},
                    follow_redirects=False)
    assert r.status_code == 403


def test_customer_orders_flow(app):
    from digisearch.web.features.catalog import repo as catrepo
    from digisearch.web.features.contacts import repo as conrepo
    from digisearch.web.features.customer_orders import repo as corepo

    db = app.state.database
    cust = conrepo.create_contact(db, {"kind": "customer", "name": "Acme AB"})
    part = catrepo.create_part(db, part={"part_no": "WIDGET-1", "value": "blue"},
                               supplier_lines=[{"supplier_name": "X", "unit_price": 2.0,
                                                "reel_qty": 1, "is_default": True}])
    client = TestClient(app)
    _login(client, "buyer1", "pw")

    r = client.post("/customer-orders/new",
                    data={"customer_id": cust, "order_ref": "SO-9", "status": "confirmed",
                          "tax_rate": "25"}, follow_redirects=False)
    assert r.status_code == 303
    oid = int(r.headers["location"].rsplit("/", 1)[1])

    # add a line — price omitted, defaults to the part's cost (2.0), qty 10
    client.post(f"/customer-orders/{oid}/lines/add", data={"part_id": part, "qty": "10"},
                follow_redirects=False)
    page = client.get(f"/customer-orders/{oid}").text
    assert "WIDGET-1" in page and "SO-9" in page

    order = corepo.get_order(db, oid)
    assert order["lines"][0]["unit_price"] == 2.0
    assert abs(order["grand_total"] - 25.0) < 1e-9  # 10*2 + 25% tax

    lid = order["lines"][0]["id"]
    client.post(f"/customer-orders/{oid}/lines/{lid}/update",
                data={"qty": "10", "unit_price": "5", "discount": "0"}, follow_redirects=False)
    assert abs(corepo.get_order(db, oid)["grand_total"] - 62.5) < 1e-9  # 50 + 25% tax

    assert "SO-9" in client.get("/customer-orders").text  # shows in the list

    client.post(f"/customer-orders/{oid}/lines/{lid}/delete", follow_redirects=False)
    assert corepo.get_order(db, oid)["lines"] == []


def test_despatch_flow(app):
    from digisearch.web.features.catalog import repo as catrepo
    from digisearch.web.features.contacts import repo as conrepo
    from digisearch.web.features.customer_orders import repo as corepo

    db = app.state.database
    part = catrepo.create_part(db, part={"part_no": "SH-1"},
                               supplier_lines=[{"supplier_name": "X", "unit_price": 1.0,
                                                "reel_qty": 1, "is_default": True}],
                               opening={"qty": 40})
    cust = conrepo.create_contact(db, {"kind": "customer", "name": "Acme"})
    oid = corepo.create_order(db, {"customer_id": cust, "order_ref": "SO-S"})
    corepo.add_line(db, oid, part, 25, 10.0, None)

    client = TestClient(app)
    _login(client, "buyer1", "pw")
    assert f"/despatch/from-order/{oid}" in client.get(f"/customer-orders/{oid}").text
    assert "SH-1" in client.get(f"/despatch/from-order/{oid}").text

    line_id = corepo.get_order(db, oid)["lines"][0]["id"]
    r = client.post(f"/despatch/from-order/{oid}",
                    data={"ship": str(line_id), f"qty_{line_id}": "25"}, follow_redirects=False)
    assert r.status_code == 303 and "/despatch/" in r.headers["location"]
    assert catrepo.get_part(db, part)["total_qty"] == 15  # 40 − 25 shipped
    assert corepo.get_order(db, oid)["status"] == "shipped"
    assert "DN-" in client.get("/despatch").text

    desp_id = int(r.headers["location"].rsplit("/", 1)[1])
    client.post(f"/despatch/{desp_id}/invoice", data={"invoice_no": "INV-9"}, follow_redirects=False)
    assert "invoiced" in client.get(f"/despatch/{desp_id}").text


def test_customer_order_allocation(app):
    from digisearch.web.features.catalog import repo as catrepo
    from digisearch.web.features.contacts import repo as conrepo
    from digisearch.web.features.customer_orders import repo as corepo

    db = app.state.database
    part = catrepo.create_part(db, part={"part_no": "AL-1"},
                               supplier_lines=[{"supplier_name": "X", "unit_price": 1.0,
                                                "reel_qty": 1, "is_default": True}],
                               opening={"qty": 50})
    cust = conrepo.create_contact(db, {"kind": "customer", "name": "Acme"})
    oid = corepo.create_order(db, {"customer_id": cust, "order_ref": "SO-A"})
    corepo.add_line(db, oid, part, 30, 10.0, None)

    client = TestClient(app)
    _login(client, "buyer1", "pw")
    client.post(f"/customer-orders/{oid}/allocate", follow_redirects=False)
    assert corepo.get_order(db, oid)["lines"][0]["allocated"] == 30
    assert catrepo.get_part(db, part)["free"] == 20  # 50 − 30 reserved
    assert "reserved to this order" in client.get(f"/customer-orders/{oid}").text

    client.post(f"/customer-orders/{oid}/release", follow_redirects=False)
    assert corepo.get_order(db, oid)["lines"][0]["allocated"] == 0


def test_cancel_customer_order_releases_allocation(app):
    from digisearch.web.features.catalog import repo as catrepo
    from digisearch.web.features.contacts import repo as conrepo
    from digisearch.web.features.customer_orders import repo as corepo

    db = app.state.database
    part = catrepo.create_part(db, part={"part_no": "CC-1"},
                               supplier_lines=[{"supplier_name": "X", "unit_price": 1.0,
                                                "reel_qty": 1, "is_default": True}],
                               opening={"qty": 40})
    cust = conrepo.create_contact(db, {"kind": "customer", "name": "Acme"})
    oid = corepo.create_order(db, {"customer_id": cust, "status": "confirmed"})
    corepo.add_line(db, oid, part, 25, 10.0, None)
    corepo.allocate_order(db, oid)
    assert catrepo.get_part(db, part)["total_alloc"] == 25

    client = TestClient(app)
    _login(client, "buyer1", "pw")
    assert f"/customer-orders/{oid}/cancel" in client.get(f"/customer-orders/{oid}").text

    client.post(f"/customer-orders/{oid}/cancel", follow_redirects=False)
    assert corepo.get_order(db, oid)["status"] == "cancelled"
    assert catrepo.get_part(db, part)["total_alloc"] == 0  # allocation rolled back
    # a cancelled order no longer offers cancel
    assert f"/customer-orders/{oid}/cancel" not in client.get(f"/customer-orders/{oid}").text


def test_cancelled_order_warns_about_downstream(app):
    from digisearch.web.features.assemblies import repo as asmrepo
    from digisearch.web.features.catalog import repo as catrepo
    from digisearch.web.features.contacts import repo as conrepo
    from digisearch.web.features.customer_orders import repo as corepo
    from digisearch.web.features.purchase_orders import repo as porepo
    from digisearch.web.features.work_orders import repo as worepo

    db = app.state.database
    comp = catrepo.create_part(db, part={"part_no": "DS-1"},
                               supplier_lines=[{"supplier_name": "V", "supplier_pno": "V1",
                                                "unit_price": 1, "reel_qty": 1, "is_default": True}])
    asm = asmrepo.create_assembly(db, {"part_no": "DSA-1"})
    asmrepo.add_bom_line(db, asm, comp, 1, None)
    cust = conrepo.create_contact(db, {"kind": "customer", "name": "Acme"})
    oid = corepo.create_order(db, {"customer_id": cust, "status": "confirmed"})
    corepo.add_line(db, oid, asm, 5, 100.0, None)
    line_id = corepo.get_order(db, oid)["lines"][0]["id"]

    wo_id = worepo.create_work_orders_for_order(db, oid, {line_id: 5}, "buyer1")[0]  # linked WO
    po_id = porepo.create_po(db, {"supplier_id": porepo.suppliers(db)[0]["id"]})
    porepo.add_line(db, po_id, comp, 5, None)                 # a PO that includes the build's component
    porepo.mark_ordered(db, po_id, "buyer1")

    ds = corepo.order_downstream(db, oid)
    assert any(w["id"] == wo_id for w in ds["work_orders"])
    assert any(p["id"] == po_id for p in ds["purchase_orders"])

    client = TestClient(app)
    _login(client, "buyer1", "pw")
    client.post(f"/customer-orders/{oid}/cancel", follow_redirects=False)
    page = client.get(f"/customer-orders/{oid}").text
    assert "not</b> cancelled" in page                        # the warning banner
    assert f"/work-orders/{wo_id}" in page and f"/purchase-orders/{po_id}" in page


def test_new_order_form_calendar_and_dates(app):
    from datetime import date

    client = TestClient(app)
    _login(client, "buyer1", "pw")
    page = client.get("/customer-orders/new").text
    assert 'id="order-cal"' in page and "FullCalendar.Calendar" in page and "/planning/events" in page
    assert client.get("/static/fullcalendar/index.global.min.js").status_code == 200
    assert date.today().isoformat() in page          # order date pre-filled with today
    assert "CO-00001" in page                        # our order ref pre-filled (predicted)
    assert "dateClick" in page and "required_date" in page  # clicking a day fills Required date


def test_customer_orders_write_requires_role(app):
    client = TestClient(app)
    _login(client, "ware1", "pw")  # warehouse — may view, may not write
    assert client.get("/customer-orders").status_code == 200
    assert client.get("/customer-orders/new").status_code == 403


def test_contacts_supplier_parts_link(app):
    import re

    from digisearch.web.features.catalog import repo as catrepo
    from digisearch.web.features.contacts import repo as crepo

    db = app.state.database
    crepo.create_contact(db, {"kind": "supplier", "name": "Digikey"})
    crepo.create_contact(db, {"kind": "customer", "name": "AcmeCo"})
    catrepo.create_part(db, part={"part_no": "PARTX", "value": "10k"},
                        supplier_lines=[{"supplier_name": "Digikey", "supplier_pno": "DK-9",
                                         "unit_price": 0.1, "reel_qty": 1, "is_default": True}])

    client = TestClient(app)
    _login(client, "buyer1", "pw")
    page = client.get("/contacts").text
    assert "/catalog/supplier?name=Digikey" in page   # supplier row has the Parts link
    assert "name=AcmeCo" not in page                  # customers don't

    sp = client.get("/catalog/supplier?name=Digikey")
    assert sp.status_code == 200 and "PARTX" in sp.text
    assert re.search(r'href="/catalog/\d+"', sp.text)  # part links to its page


def test_contacts_write_requires_role(app):
    ware = TestClient(app)
    _login(ware, "ware1", "pw")  # warehouse: may view, not edit
    assert "New Contact" not in ware.get("/contacts").text
    assert ware.get("/contacts/new", follow_redirects=False).status_code == 403
    assert ware.post("/contacts/new", data={"name": "X"}, follow_redirects=False).status_code == 403


def test_placeholder_page_renders(app):
    client = TestClient(app)
    _login(client, "buyer1", "pw")
    r = client.get("/reports")
    assert r.status_code == 200 and "Reports" in r.text and "coming soon" in r.text.lower()


def test_placeholder_respects_role(app):
    client = TestClient(app)
    _login(client, "buyer1", "pw")  # purchasing role, not admin
    # Setup & Tools is admin-only — gated at the route, not just hidden in nav.
    assert client.get("/setup", follow_redirects=False).status_code == 403
    assert "Setup &amp; Tools" not in client.get("/").text  # and not shown in the sidebar


def test_login_then_purchasing_page(app):
    client = TestClient(app)
    assert _login(client, "buyer1", "pw").status_code == 303
    r = client.get("/purchasing")
    assert r.status_code == 200 and "Purchasing" in r.text
    # The feature's nav entry is rendered for a purchasing role.
    assert 'href="/purchasing"' in r.text
    # The form must post to the live route (guards against template/route drift).
    assert 'action="/purchasing/run"' in r.text


def test_purchasing_flow_and_download(app, monkeypatch):
    monkeypatch.setattr(purchasing_router, "run_purchase", _fake_run_purchase)
    client = TestClient(app)
    _login(client, "buyer1", "pw")

    r = client.post(
        "/purchasing/run",
        files={"file": ("slice.csv", b"refdes,value\nR1,10k\n", "text/csv")},
        data={"build_qty": "10", "check_stock": "true"},
    )
    assert r.status_code == 200
    assert "MPN1" in r.text and "resolved" in r.text

    import re

    m = re.search(r'/purchasing/download/([0-9a-f]+)/([^"]+\.csv)', r.text)
    assert m, "expected a cart download link"
    dl = client.get(f"/purchasing/download/{m.group(1)}/{m.group(2)}")
    assert dl.status_code == 200 and "Digi-Key Part Number" in dl.text


def test_purchasing_blocked_for_other_role(app, monkeypatch):
    monkeypatch.setattr(purchasing_router, "run_purchase", _fake_run_purchase)
    client = TestClient(app)
    _login(client, "ware1", "pw")
    r = client.post(
        "/purchasing/run",
        files={"file": ("slice.csv", b"x", "text/csv")},
        data={"build_qty": "10"},
    )
    assert r.status_code == 403


def test_rejects_unknown_file_type(app):
    client = TestClient(app)
    _login(client, "buyer1", "pw")
    r = client.post(
        "/purchasing/run",
        files={"file": ("notes.pdf", b"x", "application/pdf")},
        data={"build_qty": "10"},
    )
    assert r.status_code == 400


def test_download_path_traversal_blocked(app):
    client = TestClient(app)
    _login(client, "buyer1", "pw")
    r = client.get("/purchasing/download/abc/..%2f..%2f..%2fetc%2fpasswd")
    assert r.status_code == 404


def test_catalog_pages_render(app):
    from digisearch.web.features.catalog import importer

    importer.import_tables(
        app.state.database,
        suppliers=[{"AddID": "2", "CoName": "Digikey", "defCurrency": "SEK"}],
        parts=[{"ItemID": "1", "MasterPNo": "GRM155", "ItemName": "10uF/10V/20%/0402",
                "Category": "CAPACITOR", "Type": "PART", "xCost": "0.1", "MinQty": "0",
                "TotalQty": "100", "TotalAllocQty": "0", "TotalOnOrderQty": "0"}],
        item_suppliers=[{"AutoID": "10", "Supplier_ItemID": "1", "SupplierID": "2",
                         "SupplierPNo": "490-X", "PriceEach": "1000", "QtyPerUOM": "10000",
                         "DefaultSupplier": "1"}],
        item_locations=[{"AutoID": "100", "LocStockID": "1", "LocLocationID": "1", "LocBIN": "KH1",
                         "LocOnHandQty": "100", "LocAllocQty": "0", "LocOnOrderQty": "0"}],
    )
    client = TestClient(app)
    _login(client, "buyer1", "pw")

    r = client.get("/catalog")
    assert r.status_code == 200 and "GRM155" in r.text
    assert 'href="/catalog"' in r.text  # nav entry visible

    import re

    m = re.search(r"/catalog/(\d+)", r.text)
    assert m, "expected a part detail link"
    d = client.get(f"/catalog/{m.group(1)}")
    assert d.status_code == 200 and "490-X" in d.text and "Suppliers" in d.text
    assert "digikey.com" in d.text   # supplier P/N links to the distributor part page


def test_add_component_button_and_create(app):
    client = TestClient(app)
    _login(client, "buyer1", "pw")
    assert "+ Add component" in client.get("/catalog").text       # button visible to writer
    assert client.get("/catalog/new").status_code == 200          # form renders

    r = client.post(
        "/catalog/new",
        data={
            "part_no": "WEBPART1", "category": "capacitor", "value": "0u1/16V/10%/0402",
            "stock_qty": "1000", "bin": "B2",
            "row_key": "r0", "supplier_id": "__new__", "new_supplier_name": "Mouser",
            "supplier_pno": "MO-1", "unit_price": "0.2", "reel_qty": "4000", "moq": "1",
            "lead_time": "5", "default_key": "r0",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"].startswith("/catalog/")

    detail = client.get(r.headers["location"])
    assert detail.status_code == 200 and "WEBPART1" in detail.text and "Mouser" in detail.text
    assert "WEBPART1" in client.get("/catalog?q=WEBPART1").text    # shows in the list


def test_add_component_requires_write_role(app):
    client = TestClient(app)
    _login(client, "ware1", "pw")  # warehouse: may browse but not add
    assert "+ Add component" not in client.get("/catalog").text
    assert client.get("/catalog/new", follow_redirects=False).status_code == 403
    r = client.post("/catalog/new", data={"part_no": "X"}, follow_redirects=False)
    assert r.status_code == 403


def _create_part(client, part_no="EDITME"):
    return client.post(
        "/catalog/new",
        data={"part_no": part_no, "category": "RESISTOR", "stock_qty": "100", "bin": "X1",
              "row_key": "r0", "supplier_id": "__new__", "new_supplier_name": "Digikey",
              "supplier_pno": "DK-1", "unit_price": "0.1", "reel_qty": "5000", "default_key": "r0"},
        follow_redirects=False,
    ).headers["location"].rsplit("/", 1)[1]


def test_edit_component_flow(app):
    from digisearch.web.features.catalog import repo as catrepo

    client = TestClient(app)
    _login(client, "buyer1", "pw")
    pid = _create_part(client)  # creates supplier "Digikey" inline
    dk_id = next(s["id"] for s in catrepo.suppliers(app.state.database) if s["name"] == "Digikey")

    # detail shows an Edit button; the edit form pre-fills and offers the supplier dropdown
    assert f"/catalog/{pid}/edit" in client.get(f"/catalog/{pid}").text
    f = client.get(f"/catalog/{pid}/edit")
    assert f.status_code == 200 and 'value="EDITME"' in f.text
    assert 'name="supplier_id"' in f.text and "Digikey" in f.text  # dropdown + existing option

    # edit: pick the existing Digikey from the dropdown (by id) and change fields
    r = client.post(
        f"/catalog/{pid}/edit",
        data={"part_no": "EDITED", "category": "CAPACITOR", "stock_qty": "250", "bin": "Y2",
              "row_key": "r0", "supplier_id": str(dk_id), "supplier_pno": "NEW-PN",
              "unit_price": "0.3", "reel_qty": "4000", "default_key": "r0"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == f"/catalog/{pid}"

    d = client.get(f"/catalog/{pid}").text
    assert "EDITED" in d and "Digikey" in d and "NEW-PN" in d and "Y2" in d
    assert "EDITME" not in d  # old part number replaced
    # the existing supplier was reused (no duplicate created)
    assert sum(s["name"] == "Digikey" for s in catrepo.suppliers(app.state.database)) == 1


def test_assemblies_pages_and_parts_split(app):
    from digisearch.web.features.assemblies import importer as asmimp
    from digisearch.web.features.catalog import importer as catimp

    db = app.state.database
    catimp.import_tables(
        db, suppliers=[],
        parts=[
            {"ItemID": "100", "MasterPNo": "ASM-100", "ItemName": "Widget", "Type": "ASSY",
             "Category": "PRODUCT", "xCost": "", "MinQty": "0", "TotalQty": "0",
             "TotalAllocQty": "0", "TotalOnOrderQty": "0"},
            {"ItemID": "1", "MasterPNo": "RES-1", "ItemName": "10k", "Type": "PART",
             "Category": "RESISTOR", "xCost": "0.1", "MinQty": "0", "TotalQty": "100",
             "TotalAllocQty": "0", "TotalOnOrderQty": "0"},
        ],
        item_suppliers=[], item_locations=[],
    )
    with db.connect() as conn:
        pm = {r["minimrp_id"]: r["id"]
              for r in conn.execute("SELECT id, minimrp_id FROM parts WHERE minimrp_id IS NOT NULL")}
    asmimp.import_bom_rows(db, parts_map=pm, usedin=[
        {"AutoID": "1", "ParentID": "100", "ChildID": "1", "QtyPer": "5", "RefText": "R1, R2",
         "LineItemNo": "1"},
    ])

    client = TestClient(app)
    _login(client, "buyer1", "pw")

    # assemblies list shows the assembly (not the component)
    al = client.get("/assemblies")
    assert al.status_code == 200 and "ASM-100" in al.text and "RES-1" not in al.text

    # assembly detail shows the BOM line linking to the component in the catalog
    d = client.get(f"/assemblies/{pm[100]}")
    assert d.status_code == 200 and "RES-1" in d.text and "R1, R2" in d.text
    assert f'/catalog/{pm[1]}' in d.text  # drill to component

    # Parts/Assemblies split: the catalog list excludes assemblies, includes components
    parts = client.get("/catalog").text
    assert "RES-1" in parts and "ASM-100" not in parts


def test_setup_tools_and_import(app, monkeypatch):
    import digisearch.web.features.setup.router as setup_router

    app.state.store.create_user("admin1", "pw", role="admin")
    monkeypatch.setattr(setup_router, "_source_path", lambda: ".")  # an existing path
    monkeypatch.setattr(setup_router, "import_from_minimrp", lambda db, p: {"parts": 5, "suppliers": 2})
    monkeypatch.setattr(setup_router, "import_boms", lambda db, p: {"bom_lines": 7})
    monkeypatch.setattr(setup_router, "import_contacts", lambda db, p: {"contacts": 9})

    # non-admin cannot reach Setup & Tools
    buyer = TestClient(app)
    _login(buyer, "buyer1", "pw")
    assert buyer.get("/setup", follow_redirects=False).status_code == 403

    admin = TestClient(app)
    _login(admin, "admin1", "pw")
    idx = admin.get("/setup")
    assert idx.status_code == 200 and "Import from miniMRP" in idx.text
    assert admin.get("/setup/import").status_code == 200

    r = admin.post("/setup/import")
    assert r.status_code == 200
    assert "bom lines" in r.text and "7" in r.text and "Import complete" in r.text


def test_setup_import_handles_missing_db(app, monkeypatch):
    import digisearch.web.features.setup.router as setup_router

    app.state.store.create_user("admin2", "pw", role="admin")
    monkeypatch.setattr(setup_router, "_source_path", lambda: "/no/such/mrp5data")
    admin = TestClient(app)
    _login(admin, "admin2", "pw")
    r = admin.post("/setup/import")  # no file uploaded, no valid configured path
    assert r.status_code == 400 and "no database selected" in r.text.lower()


def test_setup_import_with_uploaded_file(app, monkeypatch):
    import digisearch.web.features.setup.router as setup_router

    app.state.store.create_user("admin3", "pw", role="admin")
    monkeypatch.setattr(setup_router, "_source_path", lambda: None)  # no configured default
    captured = {}

    def fake_cat(db, path):
        captured["path"] = path
        return {"parts": 3}

    monkeypatch.setattr(setup_router, "import_from_minimrp", fake_cat)
    monkeypatch.setattr(setup_router, "import_boms", lambda db, p: {"bom_lines": 4})
    monkeypatch.setattr(setup_router, "import_contacts", lambda db, p: {"contacts": 2})

    admin = TestClient(app)
    _login(admin, "admin3", "pw")
    r = admin.post("/setup/import", files={"dbfile": ("mrp5data", b"FAKEDB")})
    assert r.status_code == 200 and "bom lines" in r.text and "Import complete" in r.text
    # the browsed/uploaded file was imported (a temp copy named after it), not the (None) default
    assert captured["path"] and captured["path"].endswith("mrp5data")


def _setup_assembly(app):
    from digisearch.web.features.catalog import importer as catimp
    db = app.state.database
    catimp.import_tables(
        db, suppliers=[],
        parts=[
            {"ItemID": "100", "MasterPNo": "ASM-100", "ItemName": "Widget", "Type": "ASSY",
             "Category": "PRODUCT", "xCost": "", "MinQty": "0", "TotalQty": "0",
             "TotalAllocQty": "0", "TotalOnOrderQty": "0"},
            {"ItemID": "1", "MasterPNo": "RES-1", "ItemName": "10k", "Type": "PART",
             "Category": "RESISTOR", "xCost": "0.1", "MinQty": "0", "TotalQty": "100",
             "TotalAllocQty": "0", "TotalOnOrderQty": "0"},
        ],
        item_suppliers=[], item_locations=[],
    )
    with db.connect() as conn:
        pm = {r["minimrp_id"]: r["id"]
              for r in conn.execute("SELECT id, minimrp_id FROM parts WHERE minimrp_id IS NOT NULL")}
    return pm[100], pm[1]  # assembly id, component id


def test_assembly_bom_add_and_delete(app):
    import re

    asm, comp = _setup_assembly(app)
    client = TestClient(app)
    _login(client, "buyer1", "pw")

    # top "Add Component to Assy" button + a filterable, clickable part list (dialog)
    page = client.get(f"/assemblies/{asm}")
    assert "Add Component to Assy" in page.text
    assert 'id="partsearch"' in page.text          # the filter box
    assert f'value="{comp}"' in page.text and 'class="part-row"' in page.text  # clickable part
    assert "in stock" in page.text  # each part shows its current stock level

    # add a BOM line
    r = client.post(
        f"/assemblies/{asm}/lines/add",
        data={"child_id": str(comp), "qty_per": "5", "refdes": "R1, R2"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    det = client.get(f"/assemblies/{asm}").text
    assert "RES-1" in det and "R1, R2" in det
    assert "total cost" in det and "Unit (SEK)" in det   # cost summary + per-line columns

    # delete it
    m = re.search(rf"/assemblies/{asm}/lines/(\d+)/delete", det)
    assert m, "expected a delete control for the line"
    d = client.post(f"/assemblies/{asm}/lines/{m.group(1)}/delete", follow_redirects=False)
    assert d.status_code == 303
    after = client.get(f"/assemblies/{asm}").text
    assert "R1, R2" not in after  # the line's refdes is gone (RES-1 still appears in the picker)
    assert "no BOM lines yet" in after


def test_new_assembly_flow(app):
    client = TestClient(app)
    _login(client, "buyer1", "pw")
    assert "New Assy" in client.get("/assemblies").text          # button in the stats row
    assert client.get("/assemblies/new").status_code == 200      # create form

    r = client.post(
        "/assemblies/new",
        data={"part_no": "98-NEW-9", "value": "Gadget", "rev": "A", "category": "product"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"].startswith("/assemblies/")
    nid = r.headers["location"].rsplit("/", 1)[1]
    det = client.get(f"/assemblies/{nid}").text
    assert "98-NEW-9" in det and "no BOM lines yet" in det        # new, empty assembly
    assert "98-NEW-9" in client.get("/assemblies").text           # shows in the list


def test_new_assembly_requires_write_role(app):
    ware = TestClient(app)
    _login(ware, "ware1", "pw")
    assert "New Assy" not in ware.get("/assemblies").text
    assert ware.get("/assemblies/new", follow_redirects=False).status_code == 403
    assert ware.post("/assemblies/new", data={"part_no": "X"},
                     follow_redirects=False).status_code == 403


def test_assembly_bom_edit_requires_write_role(app):
    asm, comp = _setup_assembly(app)
    ware = TestClient(app)
    _login(ware, "ware1", "pw")  # warehouse may view but not edit
    assert "Add component" not in ware.get(f"/assemblies/{asm}").text
    r = ware.post(f"/assemblies/{asm}/lines/add",
                  data={"child_id": str(comp), "qty_per": "1"}, follow_redirects=False)
    assert r.status_code == 403
    assert ware.get(f"/assemblies/{asm}/import", follow_redirects=False).status_code == 403
    assert ware.get(f"/assemblies/{asm}/edit", follow_redirects=False).status_code == 403


def test_edit_assembly_fields_flow(app):
    from digisearch.web.features.assemblies import repo as arepo

    client = TestClient(app)
    _login(client, "buyer1", "pw")
    aid = arepo.create_assembly(app.state.database, {"part_no": "EDITASM", "value": "old"})

    assert f"/assemblies/{aid}/edit" in client.get(f"/assemblies/{aid}").text  # Edit button
    f = client.get(f"/assemblies/{aid}/edit")
    assert f.status_code == 200 and 'value="EDITASM"' in f.text   # form pre-filled

    r = client.post(f"/assemblies/{aid}/edit",
                    data={"part_no": "EDITED-ASM", "value": "new", "rev": "C", "category": "product"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/assemblies/{aid}"
    det = client.get(f"/assemblies/{aid}").text
    assert "EDITED-ASM" in det and "EDITASM" not in det


def test_assembly_csv_import_flow(app, monkeypatch):
    import re

    import digisearch.web.features.assemblies.router as ar
    from digisearch.models import BomLine, Candidate, LineKind, ResolvedLine, Status
    from digisearch.web.features.purchasing.service import ResolvedRun

    asm, comp = _setup_assembly(app)  # catalog already has component "RES-1"

    def fake_resolve(path, **kw):
        return ResolvedRun(
            resolved=[
                ResolvedLine(line=BomLine(refdes=["R1"], qty=2, value="10k"), kind=LineKind.MPN,
                             chosen=Candidate(supplier="Digi-Key", mpn="RES-1",
                                              dk_part_number="RES-1-ND"), status=Status.RESOLVED),
                ResolvedLine(line=BomLine(refdes=["U1"], qty=1, value="NEWCHIP"), kind=LineKind.MPN,
                             chosen=Candidate(supplier="Mouser", mpn="NEWCHIP-XYZ",
                                              dk_part_number="81-NEWCHIP", unit_price=2.0),
                             status=Status.RESOLVED),
            ],
            build_qty=1, currency="SEK", stock_checked=False, mouser_enabled=True,
        )

    monkeypatch.setattr(ar, "resolve_bom_file", fake_resolve)

    client = TestClient(app)
    _login(client, "buyer1", "pw")
    assert "Import BOM" in client.get(f"/assemblies/{asm}").text   # button on the assembly page
    assert client.get(f"/assemblies/{asm}/import").status_code == 200

    # upload -> review screen: RES-1 already in inventory, NEWCHIP is new
    r = client.post(f"/assemblies/{asm}/import",
                    files={"file": ("bom.csv", b"refdes,value\nR1,10k\nU1,NEWCHIP\n", "text/csv")})
    assert r.status_code == 200
    assert "in inventory" in r.text and "NEWCHIP-XYZ" in r.text
    job = re.search(r'name="job_id" value="([0-9a-f]+)"', r.text).group(1)

    # apply: accept the new line (index 1); the in-inventory line is added automatically
    a = client.post(f"/assemblies/{asm}/import/apply",
                    data={"job_id": job, "accept": "1"}, follow_redirects=False)
    assert a.status_code == 303
    det = client.get(f"/assemblies/{asm}").text
    assert "RES-1" in det and "NEWCHIP-XYZ" in det   # existing linked + new created & linked


def test_edit_requires_write_role(app):
    client = TestClient(app)
    _login(client, "buyer1", "pw")
    pid = _create_part(client, "GATED")
    # warehouse may view the part but not edit it, and sees no Edit button
    ware = TestClient(app)
    _login(ware, "ware1", "pw")
    assert "/edit" not in ware.get(f"/catalog/{pid}").text
    assert ware.get(f"/catalog/{pid}/edit", follow_redirects=False).status_code == 403
