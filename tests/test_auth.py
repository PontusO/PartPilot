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
