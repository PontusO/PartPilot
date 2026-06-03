from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import digisearch.web.app as web_app
from digisearch.models import BomLine, Candidate, LineKind, ResolvedLine, Status
from digisearch.web.service import QuoteResult


@pytest.fixture
def app(tmp_path):
    application = web_app.create_app(
        db_path=tmp_path / "partpilot.db",
        data_dir=tmp_path / "data",
        secret_key="test-secret",
    )
    store = application.state.store
    store.create_user("quoter1", "pw", role="quoter")
    store.create_user("ware1", "pw", role="warehouse")
    return application


def _login(client: TestClient, username: str, password: str):
    return client.post(
        "/login", data={"username": username, "password": password}, follow_redirects=False
    )


def _fake_run_quote(bom_path, out_dir, *, build_qty=1, check_stock=True, **kw):
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
    return QuoteResult(
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
    r = _login(client, "quoter1", "nope")
    assert r.status_code == 401


def test_login_then_upload_page(app):
    client = TestClient(app)
    assert _login(client, "quoter1", "pw").status_code == 303
    r = client.get("/")
    assert r.status_code == 200 and "New quote" in r.text


def test_quote_flow_and_download(app, monkeypatch):
    monkeypatch.setattr(web_app, "run_quote", _fake_run_quote)
    client = TestClient(app)
    _login(client, "quoter1", "pw")

    r = client.post(
        "/quote",
        files={"file": ("slice.csv", b"refdes,value\nR1,10k\n", "text/csv")},
        data={"build_qty": "10", "check_stock": "true"},
    )
    assert r.status_code == 200
    assert "MPN1" in r.text and "resolved" in r.text

    # A download link is present and serves the generated file.
    import re

    m = re.search(r'/download/([0-9a-f]+)/([^"]+\.csv)', r.text)
    assert m, "expected a cart download link"
    dl = client.get(f"/download/{m.group(1)}/{m.group(2)}")
    assert dl.status_code == 200 and "Digi-Key Part Number" in dl.text


def test_quote_blocked_for_non_quote_role(app, monkeypatch):
    monkeypatch.setattr(web_app, "run_quote", _fake_run_quote)
    client = TestClient(app)
    _login(client, "ware1", "pw")
    r = client.post(
        "/quote",
        files={"file": ("slice.csv", b"x", "text/csv")},
        data={"build_qty": "10"},
    )
    assert r.status_code == 403


def test_rejects_unknown_file_type(app):
    client = TestClient(app)
    _login(client, "quoter1", "pw")
    r = client.post(
        "/quote",
        files={"file": ("notes.pdf", b"x", "application/pdf")},
        data={"build_qty": "10"},
    )
    assert r.status_code == 400


def test_download_path_traversal_blocked(app):
    client = TestClient(app)
    _login(client, "quoter1", "pw")
    r = client.get("/download/abc/..%2f..%2f..%2fetc%2fpasswd")
    assert r.status_code == 404
