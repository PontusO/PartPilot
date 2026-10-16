"""The in-app user manual (/help): rendered from docs/user-manual.md for every signed-in role."""

import pytest
from fastapi.testclient import TestClient

import partpilot.web.app as web_app
from partpilot.web.features.help import manual


@pytest.fixture
def app(tmp_path):
    a = web_app.create_app(db_path=tmp_path / "p.db", data_dir=tmp_path / "data", secret_key="s")
    a.state.store.create_user("ship", "pw", role="shipping")
    return a


def _client(app, user="ship"):
    c = TestClient(app)
    c.post("/login", data={"username": user, "password": "pw"})
    return c


def test_manual_file_exists_and_has_chapters():
    text = manual.manual_path().read_text(encoding="utf-8")
    assert text.startswith("# PartPilot user manual")
    chapters = manual.toc(text)
    assert len(chapters) >= 8
    assert any(c["slug"] == "work-orders" for c in chapters)


def test_help_page_renders_for_every_role(app):
    c = _client(app)
    r = c.get("/help")
    assert r.status_code == 200
    assert "User manual" in r.text
    assert 'id="work-orders"' in r.text          # headings become anchors for deep links
    assert 'href="#work-orders"' in r.text       # ...and appear in the table of contents


def test_manual_in_nav_and_login_required(app):
    c = _client(app)
    assert 'href="/help"' in c.get("/catalog").text
    anon = TestClient(app)
    assert anon.get("/help", follow_redirects=False).status_code in (302, 303, 307)


def test_render_markdown_makes_heading_ids():
    html = manual.render("## Work orders\n\nText.\n\n### Sub heading\n")
    assert '<h2 id="work-orders">Work orders</h2>' in html
    assert '<h3 id="sub-heading">Sub heading</h3>' in html
