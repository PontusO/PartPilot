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
    assert r.status_code == 303 and r.headers["location"].startswith("/contacts/")  # → detail page
    lst = client.get("/contacts?kind=customer").text
    assert "Acme Corp" in lst and "customer" in lst

    cid = crepo.list_contacts(app.state.database, search="Acme")[0]["id"]
    f = client.get(f"/contacts/{cid}/edit")
    assert f.status_code == 200 and 'value="Acme Corp"' in f.text
    e = client.post(f"/contacts/{cid}/edit",
                    data={"kind": "customer", "name": "Acme Inc"}, follow_redirects=False)
    assert e.status_code == 303
    assert "Acme Inc" in client.get("/contacts").text


def test_refresh_cost_tiers_route_and_detail_section(app, monkeypatch):
    from digisearch.models import Candidate
    from digisearch.web.features.catalog import cost_refresh
    from digisearch.web.features.catalog import repo as catrepo

    pid = catrepo.create_part(app.state.database, part={"part_no": "RTX-1"}, supplier_lines=[
        {"supplier_name": "Digi-Key", "supplier_pno": "RTX-1-ND", "unit_price": 0.1,
         "reel_qty": 1, "is_default": True}])
    cand = Candidate(supplier="Digi-Key", mpn="RTX-1", dk_part_number="RTX-1-ND",
                     price_breaks=[(1, 0.08), (100, 0.04)])

    class FakeDK:
        def keyword_search(self, kw, limit=5):
            return [cand]

    monkeypatch.setattr(cost_refresh, "_build_clients", lambda: (FakeDK(), None))

    client = TestClient(app)
    _login(client, "buyer1", "pw")  # purchasing -> can edit

    r = client.post(f"/catalog/{pid}/refresh-cost-tiers")
    assert r.status_code == 200 and "Updated" in r.text and "Digi-Key" in r.text

    tiers = catrepo.get_part(app.state.database, pid)["suppliers"][0]["cost_tiers"]
    assert sorted((t["break_qty"], t["unit_price"]) for t in tiers) == [(1, 0.08), (100, 0.04)]

    # The read-only cost tiers now render on the part detail (view) page.
    detail = client.get(f"/catalog/{pid}").text
    assert "Supplier cost tiers" in detail and "0.08000" in detail


def test_assembly_estimate_route(app, monkeypatch):
    from digisearch.models import Candidate
    from digisearch.web.features.assemblies import repo as asmrepo
    from digisearch.web.features.catalog import cost_refresh
    from digisearch.web.features.catalog import repo as catrepo

    db = app.state.database
    leaf = catrepo.create_part(db, part={"part_no": "EST-L"}, supplier_lines=[{
        "supplier_name": "Digi-Key", "supplier_pno": "EST-L-ND", "unit_price": 0.10, "reel_qty": 1,
        "is_default": True, "cost_tiers": [{"break_qty": 1, "unit_price": 0.10}]}], opening={"qty": 0})
    asm = asmrepo.create_assembly(db, {"part_no": "EST-ASM"})
    asmrepo.add_bom_line(db, asm, leaf, 2, "R1")

    class FakeDK:
        def keyword_search(self, kw, limit=5):
            return [Candidate(supplier="Digi-Key", mpn=kw, dk_part_number=kw, price_breaks=[(1, 0.05)])]

    monkeypatch.setattr(cost_refresh, "build_clients", lambda: (FakeDK(), None))

    client = TestClient(app)
    _login(client, "buyer1", "pw")  # purchasing -> can edit
    r = client.post(f"/assemblies/{asm}/estimate", data={"build_qty": "10"})
    # Existing cards keep pre-refresh prices; a separate set of refreshed cards is shown to compare.
    assert r.status_code == 200 and "material cost @ 10" in r.text and "Refreshed" in r.text
    assert "refreshed material @ 10" in r.text                             # the extra comparison cards
    assert catrepo.get_part(db, leaf)["unit_cost"] == pytest.approx(0.10)   # unit_cost untouched

    ware = TestClient(app)
    _login(ware, "ware1", "pw")     # warehouse -> not an assembly-write role
    assert ware.post(f"/assemblies/{asm}/estimate",
                     data={"build_qty": "1"}).status_code in (302, 303, 403)


def test_assembly_detail_build_qty_reprices_from_stored(app):
    """?build_qty=N re-prices the existing figures at that volume from stored prices — in place, no
    distributor query and no separate estimate cards."""
    from digisearch.web.features.assemblies import repo as asmrepo
    from digisearch.web.features.catalog import repo as catrepo

    db = app.state.database
    leaf = catrepo.create_part(db, part={"part_no": "BQ-L"}, supplier_lines=[{
        "supplier_name": "X", "unit_price": 1.0, "reel_qty": 1, "is_default": True,
        "cost_tiers": [{"break_qty": 1, "unit_price": 1.0}, {"break_qty": 100, "unit_price": 0.5}]}])
    asm = asmrepo.create_assembly(db, {"part_no": "BQ-A"})
    asmrepo.add_bom_line(db, asm, leaf, 1, "R1")

    client = TestClient(app)
    _login(client, "buyer1", "pw")
    base = client.get(f"/assemblies/{asm}").text            # build 1 -> unlabelled, qty-1 tier (1.00)
    assert "@ 200" not in base and "material cost (SEK)" in base
    page = client.get(f"/assemblies/{asm}?build_qty=200").text   # 200 leaves -> the 100 cost tier (0.50)
    assert "material cost @ 200" in page and "loaded build cost @ 200" in page
    assert "customer quote" not in page                     # no mfg-margin/quote layer
    assert 'value="200"' in page                            # build-vol field keeps the entered value
    assert "refreshed material" not in page                 # re-price only; extra cards are refresh-only


def test_setup_pricing_markup_screen(app):
    from digisearch.web.features.setup import repo as setup_repo

    app.state.store.create_user("boss", "pw", role="admin")
    client = TestClient(app)
    _login(client, "boss", "pw")

    page = client.get("/setup/pricing")
    assert page.status_code == 200 and "Default overhead factor" in page.text
    assert "manufacturing margin" not in page.text.lower()  # mfg-margin layer removed
    assert 'value="1.3"' in page.text                      # unset -> defaults 1.30

    r = client.post("/setup/pricing", data={"default_markup": "1.45"})
    assert r.status_code == 200 and "Saved." in r.text
    assert setup_repo.get_default_markup(app.state.database) == pytest.approx(1.45)

    # 0 / negative would zero prices -> rejected, keeps the current value.
    client.post("/setup/pricing", data={"default_markup": "0"})
    assert setup_repo.get_default_markup(app.state.database) == pytest.approx(1.45)

    # a purchasing user cannot reach setup
    other = TestClient(app)
    _login(other, "buyer1", "pw")
    assert other.get("/setup/pricing").status_code in (302, 303, 403)


def test_per_part_markup_zero_is_clamped_to_none(app):
    """A per-part markup of 0 (or negative) would zero the part's sell prices — the form clamps it to
    None so it falls back to the Setup default."""
    from digisearch.web.features.catalog import repo as catrepo

    client = TestClient(app)
    _login(client, "buyer1", "pw")
    r = client.post("/catalog/new", data={"part_no": "MK-0", "markup": "1.5"},
                    follow_redirects=False)
    pid = int(r.headers["location"].rsplit("/", 1)[-1])
    assert catrepo.get_part(app.state.database, pid)["markup"] == 1.5

    client.post(f"/catalog/{pid}/edit", data={"part_no": "MK-0", "markup": "0"},
                follow_redirects=False)
    assert catrepo.get_part(app.state.database, pid)["markup"] is None   # 0 -> None, not stored


def test_part_form_pricing_roundtrip_and_generate(app):
    """Create a part with a markup + sell tiers via the form; edit preserves cost tiers; the
    generate endpoint derives markup tiers from captured cost breaks."""
    from digisearch.web.features.catalog import repo as catrepo

    client = TestClient(app)
    _login(client, "buyer1", "pw")  # purchasing -> can edit

    r = client.post("/catalog/new", data={
        "part_no": "PRICED-1", "category": "RESISTOR", "markup": "1.5",
        "sell_break_qty": ["1", "1000"], "sell_unit_price": ["0.50", "0.30"],
    }, follow_redirects=False)
    assert r.status_code == 303
    pid = int(r.headers["location"].rsplit("/", 1)[-1])

    part = catrepo.get_part(app.state.database, pid)
    assert part["markup"] == 1.5
    assert [(t["break_qty"], t["unit_price"], t["source"]) for t in part["sell_tiers"]] == \
        [(1, 0.50, "manual"), (1000, 0.30, "manual")]

    # Give it a captured cost tier, then generate markup tiers from it.
    with app.state.database.connect() as conn:
        ps_id = conn.execute("SELECT id FROM part_suppliers WHERE part_id = ?", (pid,)).fetchone()
        # no supplier line was submitted; add one to hang a cost tier on
        ps_id = conn.execute(
            "INSERT INTO part_suppliers (part_id, supplier_id, price_per_uom, qty_per_uom, is_default)"
            " VALUES (?, NULL, 0.2, 1, 1)", (pid,)).lastrowid
        conn.execute("INSERT INTO part_supplier_tiers (part_supplier_id, break_qty, unit_price, kind)"
                     " VALUES (?, 500, 0.20, 'cut')", (ps_id,))
        conn.commit()

    g = client.post(f"/catalog/{pid}/generate-sell-tiers", follow_redirects=False)
    assert g.status_code == 303
    sell = {t["break_qty"]: t["source"] for t in catrepo.get_part(app.state.database, pid)["sell_tiers"]}
    assert sell.get(500) == "markup"          # generated at cost 0.20 x markup 1.5 = 0.30
    assert sell.get(1) == "manual" and sell.get(1000) == "manual"   # manual tiers preserved

    # The edit form renders with the overhead factor and both cost + loaded-cost tier tables.
    form = client.get(f"/catalog/{pid}/edit").text
    assert 'value="1.5"' in form and "Loaded cost tiers" in form
    assert "Captured supplier cost breaks" in form


def test_supplier_contact_mirrors_into_catalog_suppliers(app):
    """A supplier added in Contacts must appear in the part-edit / PO supplier dropdown, which
    reads the catalog ``suppliers`` table (not ``contacts``)."""
    from digisearch.web.features.catalog import repo as catrepo

    client = TestClient(app)
    _login(client, "buyer1", "pw")  # purchasing -> can edit

    db = app.state.database
    assert not any(s["name"] == "Widget Supply AB" for s in catrepo.suppliers(db))

    r = client.post("/contacts/new",
                    data={"kind": "supplier", "name": "Widget Supply AB", "currency": "SEK",
                          "website": "https://widgets.example"}, follow_redirects=False)
    assert r.status_code == 303
    mirrored = [s for s in catrepo.suppliers(db) if s["name"] == "Widget Supply AB"]
    assert len(mirrored) == 1  # now selectable on the part form

    # A non-supplier contact must NOT create a suppliers row.
    client.post("/contacts/new", data={"kind": "customer", "name": "Buyer Co"},
                follow_redirects=False)
    assert not any(s["name"] == "Buyer Co" for s in catrepo.suppliers(db))

    # Editing the same-named supplier contact updates its mirror in place (no duplicate row).
    from digisearch.web.features.contacts import repo as conrepo
    sup_cid = conrepo.list_contacts(db, search="Widget")[0]["id"]
    client.post(f"/contacts/{sup_cid}/edit",
                data={"kind": "supplier", "name": "Widget Supply AB", "currency": "USD"},
                follow_redirects=False)
    rows = [s for s in catrepo.suppliers(db) if s["name"] == "Widget Supply AB"]
    assert len(rows) == 1  # still exactly one — matched by name, updated not duplicated


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
    # a fresh WO is 'allocated' internally but shown as "Planned" (nothing reserved)
    assert "WO-77" in page and "WC-1" in page and "Planned" in page

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
    # the GRN detail page shows the price paid and line value (15 x 1.0)
    grn_url = rr.headers["location"]
    grn = client.get(grn_url).text
    assert "Unit price" in grn and "Line value" in grn and "15.00" in grn
    # and offers a printable PDF + CSV of the receipt
    assert f"{grn_url}/export.pdf" in grn and f"{grn_url}/export.csv" in grn
    rp = client.get(f"{grn_url}/export.pdf")
    assert rp.status_code == 200 and rp.content[:5] == b"%PDF-"
    rc = client.get(f"{grn_url}/export.csv")
    assert rc.status_code == 200 and "Part No" in rc.text
    # and the offer now carries that price as its "last purchase price"
    assert catrepo.get_part(db, p)["suppliers"][0]["unit_price"] == pytest.approx(1.0)


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


def test_order_settings_toggle_acknowledge_confirm(app):
    from digisearch.web.features.contacts import repo as conrepo
    from digisearch.web.features.customer_orders import repo as corepo
    from digisearch.web.features.catalog import repo as catrepo
    from digisearch.web.features.setup import repo as setuprepo

    db = app.state.database
    app.state.store.create_user("boss", "pw", role="admin")
    part = catrepo.create_part(db, part={"part_no": "OS-1"},
                               supplier_lines=[{"supplier_name": "X", "unit_price": 1.0,
                                                "reel_qty": 1, "is_default": True}])
    cust = conrepo.create_contact(db, {"kind": "customer", "name": "Acme"})

    # non-admin can't reach order settings
    buyer = TestClient(app)
    _login(buyer, "buyer1", "pw")
    assert buyer.get("/setup/orders", follow_redirects=False).status_code == 403

    admin = TestClient(app)
    _login(admin, "boss", "pw")
    assert "checked" in admin.get("/setup/orders").text          # default ON
    assert setuprepo.get_orders(db)["ack_confirms"] is True

    # turn it OFF (checkbox omitted) → acknowledging no longer confirms a draft
    assert "Saved" in admin.post("/setup/orders", data={}).text
    assert setuprepo.get_orders(db)["ack_confirms"] is False

    o_off = corepo.create_order(db, {"customer_id": cust})        # draft
    corepo.add_line(db, o_off, part, 2, 5.0, None)
    buyer.post(f"/customer-orders/{o_off}/acknowledge", follow_redirects=False)
    assert corepo.get_order(db, o_off)["status"] == "draft"       # status left alone
    assert len(corepo.documents_for_order(db, o_off)) == 1        # PDF still archived

    # turn it back ON → acknowledging confirms the draft again
    admin.post("/setup/orders", data={"ack_confirms": "1"})
    assert setuprepo.get_orders(db)["ack_confirms"] is True
    o_on = corepo.create_order(db, {"customer_id": cust})
    corepo.add_line(db, o_on, part, 2, 5.0, None)
    buyer.post(f"/customer-orders/{o_on}/acknowledge", follow_redirects=False)
    assert corepo.get_order(db, o_on)["status"] == "confirmed"


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

    # add a line — price omitted, defaults to the loaded parts price = material 2.0 x overhead 1.30
    client.post(f"/customer-orders/{oid}/lines/add", data={"part_id": part, "qty": "10"},
                follow_redirects=False)
    page = client.get(f"/customer-orders/{oid}").text
    assert "WIDGET-1" in page and "SO-9" in page

    order = corepo.get_order(db, oid)
    assert order["lines"][0]["unit_price"] == pytest.approx(2.6)
    assert abs(order["grand_total"] - 32.5) < 1e-9  # 10*2.6 + 25% tax

    lid = order["lines"][0]["id"]
    client.post(f"/customer-orders/{oid}/lines/{lid}/update",
                data={"qty": "10", "unit_price": "5", "discount": "0"}, follow_redirects=False)
    assert abs(corepo.get_order(db, oid)["grand_total"] - 62.5) < 1e-9  # 50 + 25% tax

    assert "SO-9" in client.get("/customer-orders").text  # shows in the list

    client.post(f"/customer-orders/{oid}/lines/{lid}/delete", follow_redirects=False)
    assert corepo.get_order(db, oid)["lines"] == []


def test_customer_order_acknowledgement_pdf(app):
    from digisearch.web.features.catalog import repo as catrepo
    from digisearch.web.features.contacts import repo as conrepo
    from digisearch.web.features.customer_orders import repo as corepo

    db = app.state.database
    part = catrepo.create_part(db, part={"part_no": "ACK-1", "value": "thing"},
                               supplier_lines=[{"supplier_name": "X", "unit_price": 3.0,
                                                "reel_qty": 1, "is_default": True}])
    cust = conrepo.create_contact(db, {"kind": "customer", "name": "Acme AB",
                                       "address": "1 Road", "postcode": "12345"})
    oid = corepo.create_order(db, {"customer_id": cust, "order_ref": "SO-ACK"})
    corepo.add_line(db, oid, part, 4, 10.0, None)

    client = TestClient(app)
    _login(client, "buyer1", "pw")

    # before acknowledging: the detail page offers a preview, and the PDF route serves a live preview
    page = client.get(f"/customer-orders/{oid}").text
    assert f"/customer-orders/{oid}/acknowledge" in page and "Preview acknowledgement" in page
    prev = client.get(f"/customer-orders/{oid}/acknowledgement.pdf")
    assert prev.status_code == 200 and prev.headers["content-type"] == "application/pdf"
    assert prev.content[:5] == b"%PDF-"

    # acknowledge: confirms the order and archives an immutable PDF
    r = client.post(f"/customer-orders/{oid}/acknowledge", follow_redirects=False)
    assert r.status_code == 303
    assert corepo.get_order(db, oid)["status"] == "confirmed"
    assert len(corepo.documents_for_order(db, oid)) == 1

    page2 = client.get(f"/customer-orders/{oid}").text
    assert "Order acknowledgements" in page2 and f"OA-SO-ACK.pdf" in page2  # ISO archive listed
    assert "Re-issue acknowledgement" in page2

    # the PDF route now serves the stored copy (same bytes as archived)
    served = client.get(f"/customer-orders/{oid}/acknowledgement.pdf")
    assert served.status_code == 200 and served.content == corepo.get_document(db, oid)["content"]
    assert "OA-SO-ACK.pdf" in served.headers["content-disposition"]


def test_customer_order_acknowledge_requires_role(app):
    from digisearch.web.features.contacts import repo as conrepo
    from digisearch.web.features.customer_orders import repo as corepo

    db = app.state.database
    cust = conrepo.create_contact(db, {"kind": "customer", "name": "Acme"})
    oid = corepo.create_order(db, {"customer_id": cust})
    client = TestClient(app)
    _login(client, "ware1", "pw")  # warehouse may view but not acknowledge
    assert client.post(f"/customer-orders/{oid}/acknowledge", follow_redirects=False).status_code == 403


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
    with db.connect() as conn:  # despatch is offered only for confirmed orders
        conn.execute("UPDATE customer_orders SET status = 'confirmed' WHERE id = ?", (oid,))
        conn.commit()

    client = TestClient(app)
    _login(client, "buyer1", "pw")
    assert f"/despatch/from-order/{oid}" in client.get(f"/customer-orders/{oid}").text
    assert "SH-1" in client.get(f"/despatch/from-order/{oid}").text

    from digisearch.web.features.despatch import repo as despatch_repo

    line_id = corepo.get_order(db, oid)["lines"][0]["id"]
    # 1) creating a packing list moves no stock yet
    r = client.post(f"/despatch/from-order/{oid}",
                    data={"ship": str(line_id), f"qty_{line_id}": "25"}, follow_redirects=False)
    assert r.status_code == 303 and "/despatch/" in r.headers["location"]
    desp_id = int(r.headers["location"].rsplit("/", 1)[1])
    assert catrepo.get_part(db, part)["total_qty"] == 40  # nothing shipped during packing
    pack_page = client.get(f"/despatch/{desp_id}").text
    assert "Packing list" in pack_page and "SH-1" in pack_page

    desp_line = despatch_repo.get_despatch(db, desp_id)["lines"][0]["id"]
    # 2) try to confirm without packing every line -> rejected
    bad = client.post(f"/despatch/{desp_id}/pack", data={"action": "confirm"})
    assert "Pack every item" in bad.text
    assert despatch_repo.get_despatch(db, desp_id)["status"] == "packing"

    # 3) pack the line and confirm the package is ready (still no stock moved)
    client.post(f"/despatch/{desp_id}/pack",
                data={"action": "confirm", "packed": str(desp_line)}, follow_redirects=False)
    assert despatch_repo.get_despatch(db, desp_id)["status"] == "packed"
    assert catrepo.get_part(db, part)["total_qty"] == 40

    # 4) dispatch -> now stock ships and the order is shipped
    client.post(f"/despatch/{desp_id}/dispatch", follow_redirects=False)
    assert catrepo.get_part(db, part)["total_qty"] == 15  # 40 − 25 shipped
    assert corepo.get_order(db, oid)["status"] == "shipped"
    assert "DN-" in client.get("/despatch").text

    client.post(f"/despatch/{desp_id}/invoice", data={"invoice_no": "INV-9"}, follow_redirects=False)
    assert "Invoiced" in client.get(f"/despatch/{desp_id}").text


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


def test_contact_addresses_management(app):
    from digisearch.web.features.contacts import repo as crepo

    db = app.state.database
    cid = crepo.create_contact(db, {"kind": "customer", "name": "Acme"})
    client = TestClient(app)
    _login(client, "buyer1", "pw")

    page = client.get(f"/contacts/{cid}")
    assert page.status_code == 200 and "Delivery &amp; invoice addresses" in page.text

    r = client.post(f"/contacts/{cid}/addresses/add",
                    data={"label": "Plant", "company": "Acme Plant AB", "line1": "2 St",
                          "city": "Malmo", "country": "Sweden",
                          "is_delivery": "1", "is_default_delivery": "1"}, follow_redirects=False)
    assert r.status_code == 303
    addrs = crepo.list_addresses(db, cid)
    assert len(addrs) == 1 and addrs[0]["is_default_delivery"] == 1
    aid = addrs[0]["id"]

    js = client.get(f"/contacts/{cid}/addresses.json").json()
    assert js[0]["id"] == aid and js[0]["is_delivery"] == 1 and js[0]["company"] == "Acme Plant AB"

    client.post(f"/contacts/{cid}/addresses/{aid}/edit",
                data={"label": "Plant", "line1": "2 St", "city": "Goteborg", "is_delivery": "1"},
                follow_redirects=False)
    assert crepo.get_address(db, aid)["city"] == "Goteborg"

    client.post(f"/contacts/{cid}/addresses/{aid}/delete", follow_redirects=False)
    assert crepo.list_addresses(db, cid) == []


def test_contact_address_write_requires_role(app):
    from digisearch.web.features.contacts import repo as crepo

    cid = crepo.create_contact(app.state.database, {"kind": "customer", "name": "Acme"})
    ware = TestClient(app)
    _login(ware, "ware1", "pw")  # warehouse may view but not edit
    assert ware.get(f"/contacts/{cid}").status_code == 200
    assert "+ Add address" not in ware.get(f"/contacts/{cid}").text
    r = ware.post(f"/contacts/{cid}/addresses/add", data={"label": "x"}, follow_redirects=False)
    assert r.status_code == 403


def test_contacts_write_requires_role(app):
    ware = TestClient(app)
    _login(ware, "ware1", "pw")  # warehouse: may view, not edit
    assert "New Contact" not in ware.get("/contacts").text
    assert ware.get("/contacts/new", follow_redirects=False).status_code == 403
    assert ware.post("/contacts/new", data={"name": "X"}, follow_redirects=False).status_code == 403


def test_reports_index_and_stock_ledger(app):
    from digisearch.web.features.catalog import repo, stock

    db = app.state.database
    repo.create_part(db, part={"part_no": "99-001", "value": "10uF", "description": "cap"},
                     supplier_lines=[], opening={"qty": 0})
    pid = repo.find_part_by_part_no(db, "99-001")["id"]
    stock.adjust_stock(db, pid, delta=100, mtype=stock.OPENING, reference="init", user="alice")
    stock.adjust_stock(db, pid, delta=-5, mtype=stock.WOOSALE, reference="woo-sale",
                       note="WooCommerce sale", user="auto-sync")

    client = TestClient(app)
    _login(client, "buyer1", "pw")

    idx = client.get("/reports")
    assert idx.status_code == 200 and 'href="/reports/stock-movements"' in idx.text

    led = client.get("/reports/stock-movements")
    assert led.status_code == 200
    # Both movements show; the sale carries its own WOOSALE type and the running balance (100-5=95).
    assert "WooCommerce sale" in led.text and "auto-sync" in led.text and "95" in led.text
    assert ">WOOSALE<" in led.text  # webshop sales are broken out from generic issues

    # The movement-type filter narrows to a single kind.
    sales = client.get("/reports/stock-movements?mtype=WOOSALE")
    assert sales.status_code == 200 and "WooCommerce sale" in sales.text
    assert ">OPENING<" not in sales.text  # the opening-balance row is filtered out

    # A date range with no movements comes back empty, not erroring.
    empty = client.get("/reports/stock-movements?start=2000-01-01&end=2000-01-02")
    assert empty.status_code == 200 and "No stock movements in this range." in empty.text


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


def _seed_suspect_part(app, part_no="123-4567-ND", supplier="Digikey"):
    """A part whose part_no is really a supplier order code, with no manufacturer P/N."""
    db = app.state.database
    with db.connect() as c:
        c.execute("INSERT INTO parts (part_no, value, category, mfr_pno) VALUES (?,?,?,NULL)",
                  (part_no, "10K 0402", "RESISTOR"))
        pid = c.execute("SELECT id FROM parts WHERE part_no=?", (part_no,)).fetchone()["id"]
        sid = c.execute("INSERT INTO suppliers (name) VALUES (?)", (supplier,)).lastrowid
        c.execute("INSERT INTO part_suppliers (part_id, supplier_id, supplier_pno, qty_per_uom) "
                  "VALUES (?,?,?,1)", (pid, sid, part_no))
        c.commit()
    return pid


def test_part_cleanup_lookup_and_apply(app, monkeypatch):
    import digisearch.web.features.setup.router as setup_router
    from digisearch.web.features.setup.part_cleanup import Recovery

    app.state.store.create_user("admin9", "pw", role="admin")
    pid = _seed_suspect_part(app)

    # non-admin is locked out
    buyer = TestClient(app)
    _login(buyer, "buyer1", "pw")
    assert buyer.get("/setup/part-cleanup", follow_redirects=False).status_code == 403

    admin = TestClient(app)
    _login(admin, "admin9", "pw")

    # the suspect part is listed
    page = admin.get("/setup/part-cleanup")
    assert page.status_code == 200 and "123-4567-ND" in page.text

    # apply with nothing ticked -> guarded
    assert admin.post("/setup/part-cleanup", data={"action": "apply"}).status_code == 400

    # look up (distributors mocked) fills in the recovered MPN + manufacturer
    monkeypatch.setattr(setup_router, "build_clients", lambda: (object(), None))
    monkeypatch.setattr(setup_router, "recover",
                        lambda part_no, sups, dk, mo: Recovery(mpn="RC0402FR-0710KL",
                                                               manufacturer="Yageo", source="Digi-Key"))
    looked = admin.post("/setup/part-cleanup", data={"action": "lookup", "pick": str(pid)})
    assert looked.status_code == 200 and "RC0402FR-0710KL" in looked.text and "Yageo" in looked.text

    # apply the (edited) values
    applied = admin.post("/setup/part-cleanup", data={
        "action": "apply", "pick": str(pid),
        f"mpn_{pid}": "RC0402FR-0710KL", f"mfr_{pid}": "Yageo"})
    assert applied.status_code == 200
    with app.state.database.connect() as c:
        row = c.execute("SELECT part_no, mfr_pno, mfr_name FROM parts WHERE id=?", (pid,)).fetchone()
    assert row["part_no"] == "RC0402FR-0710KL" and row["mfr_pno"] == "RC0402FR-0710KL"
    assert row["mfr_name"] == "Yageo"
    # no longer a suspect
    assert "Nothing to clean up" in admin.get("/setup/part-cleanup").text


def test_part_cleanup_skips_collision(app):
    app.state.store.create_user("admin10", "pw", role="admin")
    pid = _seed_suspect_part(app)
    # another part already owns the target MPN
    with app.state.database.connect() as c:
        c.execute("INSERT INTO parts (part_no, mfr_pno) VALUES ('EXISTING-MPN','EXISTING-MPN')")
        c.commit()

    admin = TestClient(app)
    _login(admin, "admin10", "pw")
    r = admin.post("/setup/part-cleanup",
                   data={"action": "apply", "pick": str(pid), f"mpn_{pid}": "EXISTING-MPN"})
    assert r.status_code == 200 and "already used by part" in r.text
    # the suspect part was left untouched
    with app.state.database.connect() as c:
        row = c.execute("SELECT part_no, mfr_pno FROM parts WHERE id=?", (pid,)).fetchone()
    assert row["part_no"] == "123-4567-ND" and row["mfr_pno"] is None


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
    assert "material cost" in det and "loaded build cost" in det and "Unit (SEK)" in det

    # delete it
    m = re.search(rf"/assemblies/{asm}/lines/(\d+)/delete", det)
    assert m, "expected a delete control for the line"
    d = client.post(f"/assemblies/{asm}/lines/{m.group(1)}/delete", follow_redirects=False)
    assert d.status_code == 303
    after = client.get(f"/assemblies/{asm}").text
    assert "R1, R2" not in after  # the line's refdes is gone (RES-1 still appears in the picker)
    assert "no BOM lines yet" in after


def test_assembly_bom_line_inline_edit(app):
    import re
    from digisearch.web.features.assemblies import repo as asmrepo

    asm, comp = _setup_assembly(app)
    db = app.state.database
    asmrepo.add_bom_line(db, asm, comp, 5, "R1, R2")
    line_id = asmrepo.get_assembly(db, asm)["lines"][0]["id"]

    client = TestClient(app)
    _login(client, "buyer1", "pw")

    # The row exposes an Edit control (blue .ghost button) and an edit form for the line.
    det = client.get(f"/assemblies/{asm}").text
    assert f"/assemblies/{asm}/lines/{line_id}/edit" in det
    assert 'onclick="rowEdit(' in det
    assert re.search(r'name="qty_per"[^>]*value="5"', det)

    # Editing updates qty and reference designators.
    r = client.post(f"/assemblies/{asm}/lines/{line_id}/edit",
                    data={"qty_per": "8", "refdes": "R1, R2, R3"}, follow_redirects=False)
    assert r.status_code == 303
    line = asmrepo.get_assembly(db, asm)["lines"][0]
    assert line["qty_per"] == 8 and line["refdes"] == "R1, R2, R3"

    # Zero / negative quantity is rejected (line unchanged).
    bad = client.post(f"/assemblies/{asm}/lines/{line_id}/edit",
                      data={"qty_per": "0", "refdes": "R1"}, follow_redirects=False)
    assert bad.status_code == 400
    assert asmrepo.get_assembly(db, asm)["lines"][0]["qty_per"] == 8


def test_assembly_bom_line_edit_requires_write_role(app):
    from digisearch.web.features.assemblies import repo as asmrepo

    asm, comp = _setup_assembly(app)
    db = app.state.database
    asmrepo.add_bom_line(db, asm, comp, 1, None)
    line_id = asmrepo.get_assembly(db, asm)["lines"][0]["id"]

    ware = TestClient(app)
    _login(ware, "ware1", "pw")  # warehouse may view but not edit
    assert "rowEdit(" not in ware.get(f"/assemblies/{asm}").text  # no edit controls rendered
    r = ware.post(f"/assemblies/{asm}/lines/{line_id}/edit",
                  data={"qty_per": "9"}, follow_redirects=False)
    assert r.status_code == 403
    assert asmrepo.get_assembly(db, asm)["lines"][0]["qty_per"] == 1  # unchanged


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


def test_work_order_bom_divergence_and_regenerate(app):
    from digisearch.web.features.assemblies import repo as asmrepo
    from digisearch.web.features.work_orders import repo as worepo

    asm, comp = _setup_assembly(app)                 # ASM-100 assembly, RES-1 component
    db = app.state.database
    asmrepo.add_bom_line(db, asm, comp, 2, None)     # give the assembly a BOM
    wo_id = worepo.create_work_order(db, {"assembly_id": asm, "qty": 5})

    client = TestClient(app)
    _login(client, "buyer1", "pw")

    # No divergence yet — no banner on the WO page, no badge in the list.
    assert "BOM has changed" not in client.get(f"/work-orders/{wo_id}").text
    assert "BOM changed" not in client.get("/work-orders").text

    # Rework the assembly BOM after the WO was planned.
    b_line = asmrepo.get_assembly(db, asm)["lines"][0]
    asmrepo.update_bom_line(db, asm, b_line["id"], 3, None)   # 2 → 3 per build

    det = client.get(f"/work-orders/{wo_id}").text
    assert "BOM has changed" in det and f"/work-orders/{wo_id}/regenerate-bom" in det
    assert "BOM changed" in client.get("/work-orders").text   # list badge

    # Regenerate rebuilds the component lines and clears the flag.
    r = client.post(f"/work-orders/{wo_id}/regenerate-bom", follow_redirects=False)
    assert r.status_code == 303
    wo = worepo.get_work_order(db, wo_id)
    assert wo["bom_diverged"] is False and wo["lines"][0]["qty_required"] == 15   # 3×5
    assert "BOM has changed" not in client.get(f"/work-orders/{wo_id}").text


def test_regenerate_bom_requires_work_order_role(app):
    from digisearch.web.features.assemblies import repo as asmrepo
    from digisearch.web.features.work_orders import repo as worepo

    asm, comp = _setup_assembly(app)
    db = app.state.database
    asmrepo.add_bom_line(db, asm, comp, 1, None)
    wo_id = worepo.create_work_order(db, {"assembly_id": asm, "qty": 1})

    app.state.store.create_user("ship_wo", "pw", role="shipping")  # not a work-order role
    ship = TestClient(app)
    _login(ship, "ship_wo", "pw")
    assert ship.post(f"/work-orders/{wo_id}/regenerate-bom",
                     follow_redirects=False).status_code == 403


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


# ----- webshop (WooCommerce) sync -----

class _FakeWoo:
    """Stand-in for WooClient used in the sync route tests (no network)."""
    def __init__(self, products):
        self._products = products
        self.pushed = []

    def ping(self):
        return True

    def iter_products(self):
        return iter(self._products)

    def update_stock_batch(self, updates):
        updates = list(updates)
        self.pushed.append(updates)
        return len(updates)


def test_webshop_settings_admin_only_and_roundtrip(app):
    from digisearch.web.features.setup import repo as setuprepo
    app.state.store.create_user("boss", "pw", role="admin")

    buyer = TestClient(app)
    _login(buyer, "buyer1", "pw")
    assert buyer.get("/setup/webshop", follow_redirects=False).status_code == 403

    admin = TestClient(app)
    _login(admin, "boss", "pw")
    r = admin.post("/setup/webshop", data={
        "base_url": "https://ilabs.se", "consumer_key": "ck_x",
        "consumer_secret": "cs_y", "action": "save"})
    assert r.status_code == 200 and "Saved" in r.text
    saved = setuprepo.get_webshop(app.state.database)
    assert saved["base_url"] == "https://ilabs.se" and saved["configured"] is True


def test_webshop_sync_preview_and_apply(app, monkeypatch):
    from digisearch.web.features.catalog import repo as catrepo
    from digisearch.web.features.setup import repo as setuprepo
    from digisearch.web.features.setup import router as setup_router
    from digisearch.woocommerce import WooProduct

    db = app.state.database
    app.state.store.create_user("boss", "pw", role="admin")
    catrepo.create_part(db, part={"part_no": "99-1"}, supplier_lines=[], opening={"qty": 1})
    setuprepo.save_webshop(db, {"base_url": "https://ilabs.se",
                                "consumer_key": "ck", "consumer_secret": "cs"})

    products = [WooProduct(id=1, sku="99-1", name="R", description=None, stock_quantity=20,
                           manage_stock=True, stock_status="instock", type="simple"),
                WooProduct(id=2, sku="98-9", name="Board", description=None, stock_quantity=3,
                           manage_stock=True, stock_status="instock", type="simple")]
    monkeypatch.setattr(setup_router, "_build_woo_client", lambda s: _FakeWoo(products))

    admin = TestClient(app)
    _login(admin, "boss", "pw")

    # non-admin gated
    buyer = TestClient(app)
    _login(buyer, "buyer1", "pw")
    assert buyer.post("/setup/webshop/sync", follow_redirects=False).status_code == 403

    # preview changes nothing
    r = admin.post("/setup/webshop/sync", data={"action": "preview"})
    assert r.status_code == 200 and "Preview only" in r.text
    assert catrepo.get_part(db, catrepo.find_part_by_part_no(db, "99-1")["id"])["total_qty"] == 1
    assert catrepo.find_part_by_part_no(db, "98-9") is None

    # apply writes
    r = admin.post("/setup/webshop/sync", data={"action": "apply"})
    assert r.status_code == 200 and "Sync applied" in r.text
    assert catrepo.get_part(db, catrepo.find_part_by_part_no(db, "99-1")["id"])["total_qty"] == 20
    assert catrepo.find_part_by_part_no(db, "98-9")["kind"] == "ASSY"
    assert setuprepo.get_webshop(db)["last_sync_at"]  # stamped


def test_webshop_sync_pushes_builds_back(app, monkeypatch):
    from digisearch.web.features.catalog import repo as catrepo, stock
    from digisearch.web.features.setup import repo as setuprepo
    from digisearch.web.features.setup import router as setup_router
    from digisearch.woocommerce import WooProduct

    db = app.state.database
    app.state.store.create_user("boss", "pw", role="admin")
    pid = catrepo.create_part(db, part={"part_no": "99-1"}, supplier_lines=[], opening={"qty": 50})
    setuprepo.save_webshop(db, {"base_url": "https://ilabs.se",
                                "consumer_key": "ck", "consumer_secret": "cs"})

    woo = WooProduct(id=11, sku="99-1", name="R", description=None, stock_quantity=50,
                     manage_stock=True, stock_status="instock", type="simple")
    fake = _FakeWoo([woo])
    monkeypatch.setattr(setup_router, "_build_woo_client", lambda s: fake)

    admin = TestClient(app)
    _login(admin, "boss", "pw")
    admin.post("/setup/webshop/sync", data={"action": "apply"})       # first sync -> baseline 50
    stock.adjust_stock(db, pid, delta=30, mtype=stock.BUILD, reference="WO")  # build 30 -> 80

    r = admin.post("/setup/webshop/sync", data={"action": "apply"})
    assert r.status_code == 200
    assert fake.pushed[-1] == [(11, 80.0)]                            # built qty pushed to Woo
    assert catrepo.get_part(db, pid)["webshop_synced_qty"] == 80


class _FakeFortnox:
    def __init__(self, *, new_customer="500", invoice_no="9001"):
        self.created_customers, self.created_invoices = [], []
        self._new, self._inv = new_customer, invoice_no

    def find_customer_by_orgno(self, org):
        return None

    def create_customer(self, payload):
        self.created_customers.append(payload)
        return {"CustomerNumber": self._new}

    def create_invoice(self, payload):
        self.created_invoices.append(payload)
        return {"DocumentNumber": self._inv}


def _ship_order(db, *, linked=False, org_no="556-1"):
    from digisearch.web.features.catalog import repo as catrepo
    from digisearch.web.features.contacts import repo as conrepo
    from digisearch.web.features.customer_orders import repo as corepo
    from digisearch.web.features.despatch import repo as despatch_repo
    cust = conrepo.create_contact(db, {"kind": "customer", "name": "Acme AB", "org_no": org_no})
    if linked:
        with db.connect() as conn:
            conn.execute("UPDATE contacts SET fortnox_customer_number='123' WHERE id=?", (cust,))
            conn.commit()
    pid = catrepo.create_part(db, part={"part_no": "99-1", "value": "W"}, supplier_lines=[],
                              opening={"qty": 50})
    oid = corepo.create_order(db, {"customer_id": cust})
    corepo.add_line(db, oid, pid, 2, 50.0, None)
    with db.connect() as conn:  # only a confirmed order can be packed/despatched
        conn.execute("UPDATE customer_orders SET status = 'confirmed' WHERE id = ?", (oid,))
        conn.commit()
    return cust, oid


def _pack_confirm_dispatch(client, db, oid, qty=2):
    """Drive the web flow: open a packing list, pack+confirm it, dispatch it. Returns despatch id."""
    from digisearch.web.features.despatch import repo as despatch_repo
    line_id = despatch_repo.shippable_lines(db, oid)[0]["line_id"]
    r = client.post(f"/despatch/from-order/{oid}",
                    data={"ship": str(line_id), f"qty_{line_id}": str(qty)}, follow_redirects=False)
    desp_id = int(r.headers["location"].rsplit("/", 1)[1])
    desp_line = despatch_repo.get_despatch(db, desp_id)["lines"][0]["id"]
    client.post(f"/despatch/{desp_id}/pack",
                data={"action": "confirm", "packed": str(desp_line)}, follow_redirects=False)
    client.post(f"/despatch/{desp_id}/dispatch", follow_redirects=False)
    return desp_id


def test_fortnox_settings_admin_only_and_connect_needs_config(app):
    app.state.store.create_user("boss", "pw", role="admin")
    buyer = TestClient(app)
    _login(buyer, "buyer1", "pw")
    assert buyer.get("/setup/fortnox", follow_redirects=False).status_code == 403

    admin = TestClient(app)
    _login(admin, "boss", "pw")
    # connecting before config is saved is refused
    assert admin.get("/setup/fortnox/connect", follow_redirects=False).status_code == 400
    r = admin.post("/setup/fortnox", data={"client_id": "cid", "client_secret": "sec",
                                           "redirect_uri": "https://pp/setup/fortnox/callback",
                                           "default_vat": "25"})
    assert r.status_code == 200 and "Saved" in r.text
    # now connect redirects to Fortnox's authorize URL
    c = admin.get("/setup/fortnox/connect", follow_redirects=False)
    assert c.status_code == 303 and c.headers["location"].startswith("https://apps.fortnox.se/oauth-v1/auth")


def test_contact_org_no_round_trips_through_form(app):
    from digisearch.web.features.contacts import repo as conrepo
    client = TestClient(app)
    _login(client, "buyer1", "pw")
    r = client.post("/contacts/new", data={"kind": "customer", "name": "Globex",
                                           "org_no": "556677-8899"}, follow_redirects=False)
    assert r.status_code in (303, 200)
    c = [x for x in conrepo.list_contacts(app.state.database) if x["name"] == "Globex"][0]
    assert conrepo.get_contact(app.state.database, c["id"])["org_no"] == "556677-8899"


def test_despatch_auto_invoices_when_fortnox_connected(app, monkeypatch):
    from digisearch.web.features.despatch import fortnox_invoice as fi
    from digisearch.web.features.despatch import repo as despatch_repo
    db = app.state.database
    cust, oid = _ship_order(db, linked=True)            # already linked → no confirmation needed
    fake = _FakeFortnox(invoice_no="9001")
    monkeypatch.setattr(fi, "build_client", lambda db: fake)

    client = TestClient(app)
    _login(client, "buyer1", "pw")
    desp_id = _pack_confirm_dispatch(client, db, oid)   # auto-invoices on dispatch
    d = despatch_repo.get_despatch(db, desp_id)
    assert d["invoice_no"] == "9001" and len(fake.created_invoices) == 1


def test_despatch_needs_customer_confirmation_then_confirm(app, monkeypatch):
    from digisearch.web.features.despatch import fortnox_invoice as fi
    from digisearch.web.features.despatch import repo as despatch_repo
    db = app.state.database
    cust, oid = _ship_order(db, linked=False)           # not linked, no Fortnox match → confirm first
    fake = _FakeFortnox(new_customer="777", invoice_no="9100")
    monkeypatch.setattr(fi, "build_client", lambda db: fake)

    client = TestClient(app)
    _login(client, "buyer1", "pw")
    desp_id = _pack_confirm_dispatch(client, db, oid)
    # auto-invoice on despatch stopped to ask: no invoice yet, prompt on the detail page
    detail = client.get(f"/despatch/{desp_id}").text
    assert "in Fortnox yet" in detail and "Create customer" in detail
    assert fake.created_invoices == []

    # confirm → creates customer + invoice
    r2 = client.post(f"/despatch/{desp_id}/fortnox-invoice", data={"confirm": "1"})
    assert r2.status_code == 200 and "9100" in r2.text
    assert len(fake.created_customers) == 1 and len(fake.created_invoices) == 1
    assert despatch_repo.get_despatch(db, desp_id)["invoice_no"] == "9100"


def test_external_price_shows_on_list_pages(app):
    from digisearch.web.features.assemblies import repo as asmrepo
    from digisearch.web.features.catalog import repo as catrepo
    db = app.state.database
    pid = catrepo.create_part(db, part={"part_no": "99-LIST"}, supplier_lines=[])
    aid = asmrepo.create_assembly(db, {"part_no": "98-LIST"})
    with db.connect() as conn:
        conn.execute("UPDATE parts SET external_price = 65 WHERE id IN (?, ?)", (pid, aid))
        conn.commit()

    client = TestClient(app)
    _login(client, "buyer1", "pw")
    parts_page = client.get("/catalog").text
    assert "Webshop (SEK)" in parts_page and "65.00" in parts_page
    asm_page = client.get("/assemblies").text
    assert "Webshop (SEK)" in asm_page and "65.00" in asm_page


def test_webshop_sync_blocked_when_unconfigured(app):
    app.state.store.create_user("boss", "pw", role="admin")
    admin = TestClient(app)
    _login(admin, "boss", "pw")
    r = admin.post("/setup/webshop/sync", data={"action": "apply"})
    assert r.status_code == 400 and "isn&#39;t configured" in r.text


# ----- convert an assembly to a component (fix mis-entered parts) -----

def test_convert_assembly_to_component_route(app):
    from digisearch.web.features.assemblies import repo as asmrepo
    from digisearch.web.features.catalog import repo as catrepo
    db = app.state.database
    aid = asmrepo.create_assembly(db, {"part_no": "98-OOPS", "value": "really a part"})

    client = TestClient(app)
    _login(client, "buyer1", "pw")                       # purchasing may edit assemblies
    r = client.post(f"/assemblies/{aid}/convert-to-component", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/catalog/{aid}"
    assert catrepo.get_part(db, aid)["kind"] == "PART"


def test_convert_blocked_by_work_order(app):
    from digisearch.web.features.assemblies import repo as asmrepo
    from digisearch.web.features.catalog import repo as catrepo
    db = app.state.database
    aid = asmrepo.create_assembly(db, {"part_no": "98-WO"})
    with db.connect() as conn:
        conn.execute("INSERT INTO work_orders (assembly_id, qty) VALUES (?, 1)", (aid,))
        conn.commit()

    client = TestClient(app)
    _login(client, "buyer1", "pw")
    r = client.post(f"/assemblies/{aid}/convert-to-component", follow_redirects=False)
    assert r.status_code == 400 and "work order" in r.text
    assert catrepo.get_part(db, aid)["kind"] == "ASSY"


def test_convert_requires_write_role(app):
    from digisearch.web.features.assemblies import repo as asmrepo
    db = app.state.database
    aid = asmrepo.create_assembly(db, {"part_no": "98-GATED"})
    ware = TestClient(app)
    _login(ware, "ware1", "pw")                          # warehouse can't edit assemblies
    assert ware.post(f"/assemblies/{aid}/convert-to-component",
                     follow_redirects=False).status_code == 403

