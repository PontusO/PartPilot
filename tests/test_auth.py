import pytest

from digisearch.web.auth import UserStore


def test_create_and_verify(tmp_path):
    store = UserStore(tmp_path / "u.db")
    user = store.create_user("alice", "s3cret", role="purchasing")
    assert user.username == "alice" and user.role == "purchasing"

    ok = store.verify("alice", "s3cret")
    assert ok is not None and ok.id == user.id and ok.role == "purchasing"


def test_wrong_password_and_unknown_user(tmp_path):
    store = UserStore(tmp_path / "u.db")
    store.create_user("bob", "right")
    assert store.verify("bob", "wrong") is None
    assert store.verify("nobody", "whatever") is None


def test_password_not_stored_plaintext(tmp_path):
    store = UserStore(tmp_path / "u.db")
    store.create_user("carol", "plaintextpw")
    blob = (tmp_path / "u.db").read_bytes()
    assert b"plaintextpw" not in blob


def test_unknown_role_rejected(tmp_path):
    store = UserStore(tmp_path / "u.db")
    with pytest.raises(ValueError):
        store.create_user("dan", "pw", role="wizard")


def test_count_and_get(tmp_path):
    store = UserStore(tmp_path / "u.db")
    assert store.count() == 0
    u = store.create_user("eve", "pw", role="admin")
    assert store.count() == 1
    assert store.get(u.id).username == "eve"
    assert store.get(9999) is None


def test_full_name_stored_and_returned(tmp_path):
    store = UserStore(tmp_path / "u.db")
    u = store.create_user("anna", "pw", role="purchasing", full_name="Anna Svensson")
    assert u.full_name == "Anna Svensson"
    assert store.get(u.id).full_name == "Anna Svensson"
    assert store.verify("anna", "pw").full_name == "Anna Svensson"
    assert store.list_users()[0].full_name == "Anna Svensson"


def test_update_role(tmp_path):
    store = UserStore(tmp_path / "u.db")
    u = store.create_user("frank", "pw", role="purchasing")
    store.update_role(u.id, "warehouse")
    assert store.get(u.id).role == "warehouse"
    with pytest.raises(ValueError):
        store.update_role(u.id, "wizard")


def test_set_password(tmp_path):
    store = UserStore(tmp_path / "u.db")
    u = store.create_user("gail", "old")
    store.set_password(u.id, "new")
    assert store.verify("gail", "old") is None
    assert store.verify("gail", "new") is not None


def test_deactivate_blocks_login_and_get(tmp_path):
    store = UserStore(tmp_path / "u.db")
    u = store.create_user("hugo", "pw", role="warehouse")
    store.set_active(u.id, False)
    assert store.verify("hugo", "pw") is None      # right password, but inactive
    assert store.get(u.id) is None                  # dropped from live sessions
    assert any(x.username == "hugo" and not x.active for x in store.list_users())
    store.set_active(u.id, True)
    assert store.verify("hugo", "pw") is not None
    assert store.get(u.id) is not None


def test_last_login_and_has_logged_in(tmp_path):
    store = UserStore(tmp_path / "u.db")
    u = store.create_user("iris", "pw")
    assert store.has_logged_in(u.id) is False
    store.verify("iris", "pw")
    assert store.has_logged_in(u.id) is True


def test_count_active_admins_and_delete(tmp_path):
    store = UserStore(tmp_path / "u.db")
    a = store.create_user("admin1", "pw", role="admin")
    store.create_user("admin2", "pw", role="admin")
    assert store.count_active_admins() == 2
    store.set_active(a.id, False)
    assert store.count_active_admins() == 1
    n = store.count()
    store.delete(a.id)
    assert store.count() == n - 1 and store.get(a.id) is None
