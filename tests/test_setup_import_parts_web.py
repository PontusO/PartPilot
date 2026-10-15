import pytest
from fastapi.testclient import TestClient

import partpilot.web.app as web_app
import partpilot.web.features.setup.router as setup_router
from partpilot.bom.normalize import load_bom
from partpilot.models import Candidate, LineKind, ResolvedLine, Status
from partpilot.web.features.purchasing.service import ResolvedRun


@pytest.fixture
def app(tmp_path):
    application = web_app.create_app(
        db_path=tmp_path / "partpilot.db", data_dir=tmp_path / "data", secret_key="test-secret")
    application.state.store.create_user("buyer1", "pw", role="purchasing")
    application.state.store.create_user("boss", "pw", role="admin")
    return application


def _admin(app):
    c = TestClient(app)
    c.post("/login", data={"username": "boss", "password": "pw"})
    return c


def _fake_resolve(bom_path, *, build_qty=None, database=None, mappings=None, **kw):
    """Parse the file with the given mappings (so the test proves the mapping reached the
    resolver), then hand back canned outcomes keyed on the value text."""
    lines = load_bom(bom_path, mappings)
    out = []
    for ln in lines:
        v = (ln.value or ln.device or "")
        if v == "EXIST":
            out.append(ResolvedLine(
                line=ln, kind=LineKind.MPN, status=Status.RESOLVED, confidence=1,
                chosen=Candidate(supplier="Digi-Key", mpn="EXIST", dk_part_number="E-ND")))
        elif v == "10k":
            out.append(ResolvedLine(
                line=ln, kind=LineKind.GENERIC_PASSIVE, status=Status.RESOLVED, confidence=0.9,
                chosen=Candidate(supplier="Digi-Key", mpn="RES-10K", manufacturer="Yageo",
                                 dk_part_number="R-ND", unit_price=0.01)))
        elif v == "":
            out.append(ResolvedLine(line=ln, kind=LineKind.MPN, status=Status.MANUAL))
        else:
            out.append(ResolvedLine(line=ln, kind=LineKind.MPN, status=Status.NOT_FOUND))
    return ResolvedRun(resolved=out, build_qty=build_qty or 1, currency="SEK",
                       stock_checked=False, mouser_enabled=False, warnings=["fake warning"])


CSV = ("Title row,,,\nDesignator,Wert,Value,Qty\n"
       "R1,10k,junk,1\nU1,EXIST,junk,1\nX1,odd,junk,1\nL1,,junk,1\n")


def _job_id(html: str) -> str:
    return html.split('name="job_id" value="')[1].split('"')[0]


def _select(html: str, name: str) -> str:
    return html.split(f'name="{name}"')[1].split("</select>")[0]


def test_import_parts_requires_admin(app):
    c = TestClient(app)
    c.post("/login", data={"username": "buyer1", "password": "pw"})
    assert c.get("/setup/import-parts").status_code == 403


def test_upload_shows_headers_samples_and_guesses(app):
    c = _admin(app)
    r = c.post("/setup/import-parts", files={"file": ("bom.csv", CSV, "text/csv")})
    assert r.status_code == 200
    html = r.text
    for h in ("Designator", "Wert", "Value", "Qty"):
        assert h in html
    assert "Title row" not in html.split("<table")[1]   # header detection skipped the title
    assert "10k" in html and "EXIST" in html             # sample values
    # alias guesses pre-selected: Designator->refdes, Value->value, Qty->qty; Wert -> ignore
    assert 'value="refdes" selected' in _select(html, "map_0")
    assert 'value="ignore" selected' in _select(html, "map_1")
    assert 'value="value" selected' in _select(html, "map_2")
    assert 'value="qty" selected' in _select(html, "map_3")


def test_full_flow_maps_resolves_reviews_and_applies(app, monkeypatch):
    monkeypatch.setattr(setup_router, "resolve_bom_file", _fake_resolve)
    from partpilot.web.features.catalog import repo as catrepo
    catrepo.create_part(app.state.database, part={"part_no": "EXIST"}, supplier_lines=[])

    c = _admin(app)
    r = c.post("/setup/import-parts", files={"file": ("bom.csv", CSV, "text/csv")})
    job_id = _job_id(r.text)

    # Map Wert -> value (the user corrects the guess), ignore Value.
    r = c.post("/setup/import-parts/resolve", data={
        "job_id": job_id, "map_0": "refdes", "map_1": "value", "map_2": "ignore", "map_3": "qty"})
    assert r.status_code == 200, r.text
    html = r.text
    assert "fake warning" in html
    assert "in inventory" in html and "RES-10K" in html
    assert html.count('name="accept"') == 2            # new (10k) + unresolved (odd)
    assert "manual" in html
    assert "junk" not in html                           # the ignored column never reached the resolver

    # Remembered for next time.
    r2 = c.post("/setup/import-parts", files={"file": ("bom2.csv", CSV, "text/csv")})
    assert 'value="value" selected' in _select(r2.text, "map_1")
    assert 'value="ignore" selected' in _select(r2.text, "map_2")

    # Apply: accept only the new row (index 0 in plan order R1, U1, X1, L1).
    r = c.post("/setup/import-parts/apply", data={"job_id": job_id, "accept": ["0"]})
    assert r.status_code == 200
    assert catrepo.find_part_id_by_mpn(app.state.database, "RES-10K") is not None
    assert catrepo.find_part_id_by_mpn(app.state.database, "odd") is None
    flat = r.text.replace("\n", "")
    assert "<b>1</b><small>parts created" in flat
    assert "<b>1</b><small>already in catalog" in flat
    assert "RES-10K" in r.text
    assert "Created parts (1)" in r.text and "EXIST</a>" not in r.text   # linked rows aren't listed as created


def test_mapping_validation_round_trips(app):
    c = _admin(app)
    r = c.post("/setup/import-parts", files={"file": ("bom.csv", CSV, "text/csv")})
    job_id = _job_id(r.text)
    r = c.post("/setup/import-parts/resolve", data={
        "job_id": job_id, "map_0": "value", "map_1": "value", "map_2": "ignore", "map_3": "qty"})
    assert r.status_code == 400
    assert "more than once" in r.text
    assert 'name="map_1"' in r.text          # back on the mapping screen, file still there
    # Both duplicate choices are retained so the user can see and fix the clash.
    assert 'value="value" selected' in _select(r.text, "map_0")
    assert 'value="value" selected' in _select(r.text, "map_1")
    assert 'value="qty" selected' in _select(r.text, "map_3")
    r = c.post("/setup/import-parts/resolve", data={
        "job_id": job_id, "map_0": "refdes", "map_1": "ignore", "map_2": "ignore", "map_3": "qty"})
    assert r.status_code == 400 and "Value or Device" in r.text
    assert 'value="refdes" selected' in _select(r.text, "map_0")   # choices retained


def test_resolve_failure_returns_to_mapping(app, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no creds")
    monkeypatch.setattr(setup_router, "resolve_bom_file", boom)
    c = _admin(app)
    r = c.post("/setup/import-parts", files={"file": ("bom.csv", CSV, "text/csv")})
    job_id = _job_id(r.text)
    r = c.post("/setup/import-parts/resolve", data={"job_id": job_id, "map_1": "value"})
    assert r.status_code == 500 and "Resolve failed: no creds" in r.text
    assert 'value="value" selected' in _select(r.text, "map_1")


def test_bad_upload_rejected(app):
    c = _admin(app)
    r = c.post("/setup/import-parts", files={"file": ("x.pdf", b"%PDF", "application/pdf")})
    assert r.status_code == 400 and "Unsupported" in r.text
    r = c.post("/setup/import-parts", data={"build_qty": "5"})
    assert r.status_code == 400
    # A corrupt Excel file is a 400 with the reader's message, and leaves nothing in jobs_dir.
    r = c.post("/setup/import-parts", files={"file": ("bad.xlsx", b"not a zip", "application/octet-stream")})
    assert r.status_code == 400 and "bad.xlsx" in r.text or r.status_code == 400
    assert not list(app.state.jobs_dir.glob("pimp-*"))


def test_expired_job_handled(app):
    c = _admin(app)
    r = c.post("/setup/import-parts/resolve", data={"job_id": "deadbeef", "map_0": "value"})
    assert r.status_code == 400 and "expired" in r.text.lower()
    r = c.post("/setup/import-parts/apply", data={"job_id": "deadbeef"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup/import-parts"
    r = c.post("/setup/import-parts/apply", data={"job_id": "../etc"}, follow_redirects=False)
    assert r.status_code == 303


def test_setup_index_lists_tool(app):
    c = _admin(app)
    r = c.get("/setup")
    assert "/setup/import-parts" in r.text
