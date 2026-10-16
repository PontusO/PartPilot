import pytest
from fastapi.testclient import TestClient

import partpilot.web.app as web_app
import partpilot.web.features.setup.router as setup_router
from partpilot.bom.normalize import load_bom
from partpilot.models import LineKind, ResolvedLine, Status
from partpilot.web.features.assemblies import repo as asmrepo
from partpilot.web.features.catalog import repo as catrepo
from partpilot.web.features.catalog.stock_index import build_stock_index
from partpilot.web.features.contacts import repo as conrepo
from partpilot.web.features.purchasing.service import ResolvedRun


@pytest.fixture
def app(tmp_path):
    a = web_app.create_app(db_path=tmp_path / "p.db", data_dir=tmp_path / "data", secret_key="s")
    a.state.store.create_user("boss", "pw", role="admin")
    return a


def _c(app):
    c = TestClient(app)
    c.post("/login", data={"username": "boss", "password": "pw"})
    return c


def _world(db):
    cust = conrepo.create_contact(db, {"kind": "customer", "name": "Acme"})
    shelf = catrepo.create_shelf(db, kind="customer", owner_contact_id=cust)
    pid = catrepo.create_part(
        db, part={"part_no": "RC0603FR-071KL", "mfr_pno": "RC0603FR-071KL", "category": "RESISTOR",
                  "value": "1K/1%/0.1W/0603"},
        supplier_lines=[], opening={"qty": 3000})
    return pid, shelf


def test_stock_index_is_built_from_chosen_shelves_only(app):
    db = app.state.database
    pid, shelf = _world(db)
    main = catrepo.default_shelf_id(db)
    assert build_stock_index(db, [main]).match_mpn("RC0603FR-071KL").free == 3000
    assert build_stock_index(db, [shelf]).match_mpn("RC0603FR-071KL").free == 0   # listed, nothing free
    assert build_stock_index(db, []).match_mpn("RC0603FR-071KL") is None


CSV = "Designator,Value,Qty\nR1,RC0603FR-071KL,1\n"
MAP = {"map_0": "refdes", "map_1": "device", "map_2": "qty"}


def _spy(seen, shelf_name):
    def spy(bom_path, **kw):
        seen.update(kw)
        lines = load_bom(bom_path, kw.get("mappings"))
        return ResolvedRun(
            resolved=[ResolvedLine(line=ln, kind=LineKind.MPN, status=Status.NOT_FOUND) for ln in lines],
            build_qty=1, currency="SEK", stock_checked=kw.get("check_stock", True),
            mouser_enabled=False, warnings=[], stock_shelf=shelf_name if kw.get("check_stock", True) else None)
    return spy


def test_upload_pages_offer_shelf_selector_and_pass_it(app, monkeypatch):
    db = app.state.database
    pid, shelf = _world(db)
    seen = {}
    monkeypatch.setattr(setup_router, "resolve_bom_file", _spy(seen, "Acme"))
    c = _c(app)
    html = c.get("/setup/import-parts").text
    assert 'name="stock_shelf"' in html and "Customer: Acme" in html and "Don't check stock" in html
    assert "ignore_stock" not in html
    r = c.post("/setup/import-parts", files={"file": ("b.csv", CSV, "text/csv")},
               data={"stock_shelf": str(shelf)})
    assert r.status_code == 200 and "Acme" in r.text
    job = r.text.split('name="job_id" value="')[1].split('"')[0]
    r = c.post("/setup/import-parts/resolve", data={"job_id": job, **MAP})
    assert seen["stock_location_id"] == shelf and seen["check_stock"] is True
    assert "Stock checked on <b>Acme</b>" in r.text


def test_upload_none_skips_stock(app, monkeypatch):
    db = app.state.database
    _world(db)
    seen = {}
    monkeypatch.setattr(setup_router, "resolve_bom_file", _spy(seen, None))
    c = _c(app)
    r = c.post("/setup/import-parts", files={"file": ("b.csv", CSV, "text/csv")},
               data={"stock_shelf": "none"})
    job = r.text.split('name="job_id" value="')[1].split('"')[0]
    r = c.post("/setup/import-parts/resolve", data={"job_id": job, **MAP})
    assert seen["check_stock"] is False and seen["stock_location_id"] is None
    assert "Stock not checked" in r.text


def test_upload_rejects_retired_shelf(app):
    db = app.state.database
    pid, shelf = _world(db)
    with db.connect() as conn:
        conn.execute("UPDATE stock_locations SET retired_at = datetime('now') WHERE id = ?", (shelf,))
        conn.commit()
    r = _c(app).post("/setup/import-parts", files={"file": ("b.csv", CSV, "text/csv")},
                     data={"stock_shelf": str(shelf)})
    assert r.status_code == 400


def test_linked_row_shows_free_on_chosen_shelf(app, monkeypatch):
    """A row whose MPN is already in the catalog links to that part, and the review shows how many
    are free on the shelf being checked — 0 on Acme's shelf even though Main holds 3000."""
    db = app.state.database
    pid, shelf = _world(db)

    def spy(bom_path, **kw):
        from partpilot.models import Candidate
        lines = load_bom(bom_path, kw.get("mappings"))
        return ResolvedRun(
            resolved=[ResolvedLine(line=ln, kind=LineKind.MPN, status=Status.RESOLVED, confidence=1,
                                   chosen=Candidate(supplier="Digi-Key", mpn="RC0603FR-071KL"))
                      for ln in lines],
            build_qty=1, currency="SEK", stock_checked=True, mouser_enabled=False, warnings=[],
            stock_shelf="Acme")
    monkeypatch.setattr(setup_router, "resolve_bom_file", spy)
    c = _c(app)
    r = c.post("/setup/import-parts", files={"file": ("b.csv", CSV, "text/csv")},
               data={"stock_shelf": str(shelf)})
    job = r.text.split('name="job_id" value="')[1].split('"')[0]
    r = c.post("/setup/import-parts/resolve", data={"job_id": job, **MAP})
    assert "in catalog" in r.text and "0 free on Acme" in r.text


def test_purchasing_and_assembly_pages_have_selector(app):
    db = app.state.database
    _world(db)
    asm = asmrepo.create_assembly(db, {"part_no": "PROD"})
    c = _c(app)
    purchasing = c.get("/purchasing").text
    assert 'name="stock_shelf"' in purchasing and 'name="check_stock"' not in purchasing
    assert 'name="stock_shelf"' in c.get(f"/assemblies/{asm}/import").text
