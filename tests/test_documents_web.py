import re

from fastapi.testclient import TestClient

import digisearch.web.app as web_app
from digisearch.web.features.article_register import repo as ar_repo
from digisearch.web.features.article_register.codes import article_code
from digisearch.web.features.documents import repo as doc_repo


def _app(tmp_path):
    app = web_app.create_app(db_path=tmp_path / "p.db", data_dir=tmp_path / "data",
                             secret_key="test-secret")
    app.state.store.create_user("buyer", "pw", role="purchasing")
    app.state.store.create_user("wh", "pw", role="warehouse")
    app.state.store.create_user("boss", "pw", role="admin")
    return app


def _login(app, username):
    client = TestClient(app)
    client.post("/login", data={"username": username, "password": "pw"}, follow_redirects=False)
    return client


def _create_file_doc(client, *, prefix="54", title="Schematic", body=b"%PDF-1.4 body",
                     filename="sch.pdf"):
    return client.post(
        "/documents/new",
        data={"mode": "new", "prefix": prefix, "title": title, "storage_kind": "file", "rev": "A"},
        files={"file": (filename, body, "application/pdf")}, follow_redirects=False)


def test_list_requires_login(tmp_path):
    app = _app(tmp_path)
    client = TestClient(app)
    r = client.get("/documents", follow_redirects=False)
    assert r.status_code in (302, 303) and "/login" in r.headers["location"]


def test_create_requires_write_role(tmp_path):
    app = _app(tmp_path)
    r = _login(app, "wh").get("/documents/new", follow_redirects=False)
    assert r.status_code == 403


def test_upload_file_document_end_to_end(tmp_path):
    app = _app(tmp_path)
    client = _login(app, "buyer")
    r = _create_file_doc(client, body=b"%PDF-1.4 hello")
    assert r.status_code == 303
    loc = r.headers["location"]
    detail = client.get(loc).text
    assert "Schematic" in detail and "sch.pdf" in detail
    # appears on the list too
    assert "Schematic" in client.get("/documents").text
    # download the revision → forced attachment, exact bytes
    m = re.search(r"/documents/\d+/revisions/\d+/download", detail)
    assert m
    dl = client.get(m.group(0))
    assert dl.status_code == 200 and dl.content == b"%PDF-1.4 hello"
    assert "attachment" in dl.headers["content-disposition"]
    assert dl.headers["content-type"] == "application/octet-stream"


def test_second_revision_becomes_current(tmp_path):
    app = _app(tmp_path)
    client = _login(app, "buyer")
    loc = _create_file_doc(client).headers["location"]
    doc_id = int(loc.rsplit("/", 1)[1])
    client.post(f"/documents/{doc_id}/revisions",
                data={"rev": "B"}, files={"file": ("v2.pdf", b"v2", "application/pdf")},
                follow_redirects=False)
    with app.state.database.connect() as conn:
        rows = conn.execute("SELECT rev, is_current FROM document_revisions "
                            "WHERE document_id = ? ORDER BY id", (doc_id,)).fetchall()
    assert [(r["rev"], r["is_current"]) for r in rows] == [("A", 0), ("B", 1)]


def test_download_path_traversal_blocked(tmp_path):
    app = _app(tmp_path)
    client = _login(app, "buyer")
    loc = _create_file_doc(client).headers["location"]
    doc_id = int(loc.rsplit("/", 1)[1])
    with app.state.database.connect() as conn:
        rev_id = conn.execute("SELECT id FROM document_revisions WHERE document_id = ?",
                              (doc_id,)).fetchone()["id"]
        conn.execute("UPDATE document_revisions SET rel_path = ? WHERE id = ?",
                     ("../../../../etc/passwd", rev_id))
        conn.commit()
    r = client.get(f"/documents/{doc_id}/revisions/{rev_id}/download")
    assert r.status_code == 404


def test_upload_role_blocked(tmp_path):
    app = _app(tmp_path)
    loc = _create_file_doc(_login(app, "buyer")).headers["location"]
    doc_id = int(loc.rsplit("/", 1)[1])
    r = _login(app, "wh").post(f"/documents/{doc_id}/revisions",
                               data={"rev": "B"}, files={"file": ("x", b"x", "text/plain")},
                               follow_redirects=False)
    assert r.status_code == 403


def test_link_document_has_no_file_download(tmp_path):
    app = _app(tmp_path)
    client = _login(app, "buyer")
    r = client.post("/documents/new",
                    data={"mode": "new", "prefix": "95", "title": "Firmware", "storage_kind": "link",
                          "external_url": "https://github.com/org/repo", "ext_ref": "main", "rev": "A"},
                    follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["location"]
    detail = client.get(loc).text
    assert "github.com/org/repo" in detail
    assert "/download" not in detail  # link docs expose no file download
    # hitting a download URL for the link revision 404s
    doc_id = int(loc.rsplit("/", 1)[1])
    with app.state.database.connect() as conn:
        rev_id = conn.execute("SELECT id FROM document_revisions WHERE document_id = ?",
                              (doc_id,)).fetchone()["id"]
    assert client.get(f"/documents/{doc_id}/revisions/{rev_id}/download").status_code == 404


def test_software_prefix_forces_link_even_if_file_posted(tmp_path):
    # A 95 (source) doc is always a link; a stray file/storage_kind=file must not create a file doc.
    app = _app(tmp_path)
    client = _login(app, "buyer")
    r = client.post("/documents/new",
                    data={"mode": "new", "prefix": "95", "title": "FW", "storage_kind": "file",
                          "external_url": "https://github.com/o/r", "rev": "A"},
                    follow_redirects=False)
    assert r.status_code == 303
    doc_id = int(r.headers["location"].rsplit("/", 1)[1])
    with app.state.database.connect() as conn:
        kind = conn.execute("SELECT storage_kind FROM documents WHERE id = ?", (doc_id,)).fetchone()[0]
    assert kind == "link"


def test_hard_delete_admin_only(tmp_path):
    app = _app(tmp_path)
    buyer = _login(app, "buyer")
    loc = _create_file_doc(buyer).headers["location"]
    doc_id = int(loc.rsplit("/", 1)[1])
    # a non-admin write user cannot delete
    assert buyer.post(f"/documents/{doc_id}/delete", follow_redirects=False).status_code == 403
    doc_dir = app.state.documents_dir / str(doc_id)
    assert doc_dir.exists()
    # admin can, and the files are removed
    r = _login(app, "boss").post(f"/documents/{doc_id}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert not doc_dir.exists()


def test_create_document_bound_to_existing_code(tmp_path):
    app = _app(tmp_path)
    db = app.state.database
    client = _login(app, "buyer")
    rn = ar_repo.create_product(db, product="Board", prefixes=["54", "99"])  # 54 doc + 99 part
    code = article_code("54", rn, 1)
    with db.connect() as conn:
        before = conn.execute("SELECT COUNT(*) FROM article_numbers").fetchone()[0]

    r = client.post("/documents/new",
                    data={"code": code, "title": "Schematic", "storage_kind": "file", "rev": "A"},
                    files={"file": ("sch.pdf", b"%PDF-1.4 x", "application/pdf")},
                    follow_redirects=False)
    assert r.status_code == 303
    doc = doc_repo.document_for_code(db, code)
    assert doc is not None and doc["code"] == code and doc["running_no"] == rn
    with db.connect() as conn:  # bound create must NOT allocate a new article number
        assert conn.execute("SELECT COUNT(*) FROM article_numbers").fetchone()[0] == before


def test_bound_create_rejects_duplicate_document(tmp_path):
    app = _app(tmp_path)
    db = app.state.database
    client = _login(app, "buyer")
    rn = ar_repo.create_product(db, product="Board", prefixes=["54"])
    code = article_code("54", rn, 1)
    client.post("/documents/new",
                data={"code": code, "title": "Schematic", "storage_kind": "file", "rev": "A"},
                files={"file": ("a.pdf", b"a", "application/pdf")}, follow_redirects=False)
    # a second document for the same number is refused, and the GET form redirects to the existing doc
    r = client.get(f"/documents/new?code={code}", follow_redirects=False)
    assert r.status_code == 303 and "/edit" in r.headers["location"]


def test_family_page_offers_create_then_edit_buttons(tmp_path):
    app = _app(tmp_path)
    db = app.state.database
    client = _login(app, "buyer")
    rn = ar_repo.create_product(db, product="Board", prefixes=["54", "99"])
    doc_code, part_code = article_code("54", rn, 1), article_code("99", rn, 1)

    page = client.get(f"/article-register/{rn}").text
    assert f"/documents/new?code={doc_code}" in page and "Create document" in page
    assert f"/catalog/new?part_no={part_code}" in page and "Create part" in page

    # materialize both, then the buttons flip to Edit
    client.post("/documents/new",
                data={"code": doc_code, "title": "Sch", "storage_kind": "file", "rev": "A"},
                files={"file": ("a.pdf", b"a", "application/pdf")}, follow_redirects=False)
    client.post("/catalog/new", data={"part_no": part_code, "value": "R 10k"},
                follow_redirects=False)
    page = client.get(f"/article-register/{rn}").text
    assert "Edit document" in page and "Edit part" in page


def test_create_links_carry_product_description(tmp_path):
    app = _app(tmp_path)
    db = app.state.database
    client = _login(app, "buyer")
    rn = ar_repo.create_product(db, product="Gateway board", prefixes=["54", "99"])
    doc_code, part_code = article_code("54", rn, 1), article_code("99", rn, 1)

    page = client.get(f"/article-register/{rn}").text
    assert ("title=Gateway%20board" in page) or ("title=Gateway+board" in page)  # → document title
    assert ("value=Gateway%20board" in page) or ("value=Gateway+board" in page)  # → part value/spec

    # the document create form prefills its title field
    form = client.get("/documents/new", params={"code": doc_code, "title": "Gateway board"}).text
    assert 'value="Gateway board"' in form
    # the part create form prefills its value/spec field
    pform = client.get("/catalog/new", params={"part_no": part_code, "value": "Gateway board"}).text
    assert 'value="Gateway board"' in pform


def test_from_template_creates_document_items_for_document_lines(tmp_path):
    from digisearch.web.features.catalog import repo as crepo

    app = _app(tmp_path)
    db = app.state.database
    client = _login(app, "buyer")
    # plain "New product from template" (no return_to): the seeded template's 54 lines are documents.
    r = client.post("/article-register/from-template",
                    data={"template_id": "1", "product": "GW", "mode": "new"},
                    follow_redirects=False)
    assert r.status_code == 303
    rn = int(r.headers["location"].rsplit("/", 1)[1])
    for i in (1, 2, 3):
        code = article_code("54", rn, i)
        assert doc_repo.document_for_code(db, code) is not None   # a document, not…
        assert crepo.find_part_by_part_no(db, code) is None       # …a catalog part


def test_article_register_detail_shows_documents_panel(tmp_path):
    app = _app(tmp_path)
    client = _login(app, "buyer")
    loc = _create_file_doc(client, title="Board schematic").headers["location"]
    doc_id = int(loc.rsplit("/", 1)[1])
    with app.state.database.connect() as conn:
        running_no = conn.execute("SELECT running_no FROM documents WHERE id = ?",
                                  (doc_id,)).fetchone()[0]
    page = client.get(f"/article-register/{running_no}").text
    assert "Board schematic" in page and f"/documents/{doc_id}" in page
