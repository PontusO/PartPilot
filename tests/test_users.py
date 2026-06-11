import pytest
from fastapi.testclient import TestClient

import digisearch.web.app as web_app


@pytest.fixture
def app(tmp_path):
    application = web_app.create_app(
        db_path=tmp_path / "partpilot.db",
        data_dir=tmp_path / "data",
        secret_key="test-secret",
    )
    store = application.state.store
    store.create_user("buyer1", "pw", role="purchasing")
    store.create_user("boss", "pw", role="admin")
    return application


def _login(client: TestClient, username: str, password: str):
    return client.post(
        "/login", data={"username": username, "password": password}, follow_redirects=False
    )


def test_users_page_admin_only(app):
    buyer = TestClient(app)
    _login(buyer, "buyer1", "pw")
    assert buyer.get("/setup/users", follow_redirects=False).status_code == 403

    admin = TestClient(app)
    _login(admin, "boss", "pw")
    page = admin.get("/setup/users")
    assert page.status_code == 200 and "boss" in page.text and "Add a user" in page.text
    # the Users tool is listed on the Setup hub
    assert "/setup/users" in admin.get("/setup").text


def test_add_user_then_login(app):
    admin = TestClient(app)
    _login(admin, "boss", "pw")
    r = admin.post("/setup/users",
                   data={"username": "anna", "full_name": "Anna Svensson",
                         "password": "topsecret", "role": "warehouse"})
    assert r.status_code == 200 and "added" in r.text and "Anna Svensson" in r.text

    fresh = TestClient(app)
    assert _login(fresh, "anna", "topsecret").status_code == 303
    assert app.state.store.verify("anna", "topsecret").role == "warehouse"


def test_duplicate_username_shows_error(app):
    admin = TestClient(app)
    _login(admin, "boss", "pw")
    admin.post("/setup/users", data={"username": "dup", "password": "x", "role": "purchasing"})
    r = admin.post("/setup/users", data={"username": "dup", "password": "y", "role": "purchasing"})
    assert r.status_code == 400 and "already exists" in r.text  # no 500


def test_change_role_and_reset_password(app):
    store = app.state.store
    uid = store.create_user("carl", "pw", role="purchasing").id
    admin = TestClient(app)
    _login(admin, "boss", "pw")

    admin.post(f"/setup/users/{uid}/role", data={"role": "shipping"})
    assert store.get(uid).role == "shipping"

    admin.post(f"/setup/users/{uid}/password", data={"password": "brandnew"})
    assert store.verify("carl", "pw") is None and store.verify("carl", "brandnew") is not None


def test_deactivate_blocks_login_then_reactivate(app):
    store = app.state.store
    uid = store.create_user("dora", "pw", role="warehouse").id
    admin = TestClient(app)
    _login(admin, "boss", "pw")

    admin.post(f"/setup/users/{uid}/active", data={"active": "0"})
    assert _login(TestClient(app), "dora", "pw").status_code == 401

    admin.post(f"/setup/users/{uid}/active", data={"active": "1"})
    assert _login(TestClient(app), "dora", "pw").status_code == 303


def test_cannot_deactivate_or_delete_self(app):
    store = app.state.store
    boss_id = store.verify("boss", "pw").id
    admin = TestClient(app)
    _login(admin, "boss", "pw")

    r = admin.post(f"/setup/users/{boss_id}/active", data={"active": "0"})
    assert r.status_code == 400 and store.get(boss_id).active is True

    r2 = admin.post(f"/setup/users/{boss_id}/delete")
    assert r2.status_code == 400 and store.get(boss_id) is not None

    r3 = admin.post(f"/setup/users/{boss_id}/role", data={"role": "purchasing"})
    assert r3.status_code == 400 and store.get(boss_id).role == "admin"


def test_delete_only_for_never_logged_in(app):
    store = app.state.store
    never = store.create_user("never", "pw", role="purchasing").id
    used = store.create_user("used", "pw", role="purchasing").id
    store.verify("used", "pw")  # this account has now logged in

    admin = TestClient(app)
    _login(admin, "boss", "pw")

    assert admin.post(f"/setup/users/{used}/delete").status_code == 400
    assert store.get(used) is not None
    assert admin.post(f"/setup/users/{never}/delete").status_code == 200
    assert store.get(never) is None


def test_self_service_password_change(app):
    client = TestClient(app)
    _login(client, "buyer1", "pw")
    assert client.get("/account/password").status_code == 200
    # link is offered in the shell
    assert "/account/password" in client.get("/").text

    bad = client.post("/account/password",
                      data={"current": "wrong", "new_password": "n2", "confirm": "n2"})
    assert bad.status_code == 400 and "incorrect" in bad.text

    mismatch = client.post("/account/password",
                           data={"current": "pw", "new_password": "a", "confirm": "b"})
    assert mismatch.status_code == 400 and "match" in mismatch.text

    ok = client.post("/account/password",
                     data={"current": "pw", "new_password": "newpw", "confirm": "newpw"})
    assert ok.status_code == 200 and "changed" in ok.text.lower()
    assert _login(TestClient(app), "buyer1", "newpw").status_code == 303
