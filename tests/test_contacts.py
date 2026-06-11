import pytest

from digisearch.web.core import FeatureRegistry
from digisearch.web.core.db import Database
from digisearch.web.features.contacts import feature as contacts_feature
from digisearch.web.features.contacts import importer, repo


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "c.db")
    reg = FeatureRegistry()
    reg.register(contacts_feature)
    database.apply_migrations(reg)
    return database


def test_import_contact_rows_maps_and_is_idempotent(db):
    rows = [
        {"AddID": "1", "CoName": "Digikey", "ShortNm": "DIGI", "defCurrency": "SEK",
         "Add1": "1 Main St", "Add2": "Town", "PCode": "12345", "Email": "a@dk.com", "Tel1": "555"},
        {"AddID": "2", "CoName": "Farnell", "ShortNm": "FAR", "defCurrency": "SEK"},
    ]
    assert importer.import_contact_rows(db, kind="supplier", source="sup", rows=rows) == 2
    by = {c["name"]: c for c in repo.list_contacts(db)}
    assert set(by) == {"Digikey", "Farnell"}
    dk = repo.get_contact(db, by["Digikey"]["id"])
    assert dk["kind"] == "supplier" and dk["email"] == "a@dk.com"
    assert dk["address"] == "1 Main St\nTown" and dk["postcode"] == "12345"

    importer.import_contact_rows(db, kind="supplier", source="sup", rows=rows)  # re-run
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 2  # upsert, no dup


def test_import_handles_email_capitalisation(db):
    importer.import_contact_rows(db, kind="customer", source="cus",
                                 rows=[{"AddID": "1", "CoName": "Cust", "EMail": "c@x.com"}])
    c = repo.list_contacts(db, kind="customer")[0]
    assert repo.get_contact(db, c["id"])["email"] == "c@x.com"


def test_same_addid_different_source_no_collision(db):
    importer.import_contact_rows(db, kind="supplier", source="sup",
                                 rows=[{"AddID": "1", "CoName": "Sup1"}])
    importer.import_contact_rows(db, kind="customer", source="cus",
                                 rows=[{"AddID": "1", "CoName": "Cust1"}])
    assert repo.summary(db) == {"total": 2, "suppliers": 1, "customers": 1, "other": 0}


def test_create_update_and_summary(db):
    cid = repo.create_contact(db, {"kind": "customer", "name": "Acme", "email": "a@acme.com",
                                   "discount": 5.0})
    c = repo.get_contact(db, cid)
    assert c["kind"] == "customer" and c["name"] == "Acme" and c["discount"] == 5.0

    repo.update_contact(db, cid, {"kind": "customer", "name": "Acme Inc", "discount": 10.0})
    c2 = repo.get_contact(db, cid)
    assert c2["name"] == "Acme Inc" and c2["discount"] == 10.0 and c2["email"] is None  # wiped

    assert repo.summary(db)["customers"] == 1


def test_list_filter_and_search(db):
    repo.create_contact(db, {"kind": "supplier", "name": "SupOne"})
    repo.create_contact(db, {"kind": "customer", "name": "CustOne"})
    assert {c["name"] for c in repo.list_contacts(db, kind="supplier")} == {"SupOne"}
    assert {c["name"] for c in repo.list_contacts(db, search="cust")} == {"CustOne"}


def test_country_persists_on_contact(db):
    cid = repo.create_contact(db, {"kind": "customer", "name": "Globex", "country": "Sweden"})
    assert repo.get_contact(db, cid)["country"] == "Sweden"
    repo.update_contact(db, cid, {"kind": "customer", "name": "Globex", "country": "Norway"})
    assert repo.get_contact(db, cid)["country"] == "Norway"


def test_address_crud_and_usage(db):
    cid = repo.create_contact(db, {"kind": "customer", "name": "Acme"})
    aid = repo.create_address(db, cid, {"label": "HQ", "company": "Acme Invoicing AB",
                                        "line1": "1 Rd", "city": "Gbg", "country": "Sweden",
                                        "is_invoice": 1})
    a = repo.get_address(db, aid)
    assert a["company"] == "Acme Invoicing AB" and a["is_invoice"] == 1 and a["is_delivery"] == 0
    assert repo.list_addresses(db, cid)[0]["id"] == aid

    repo.update_address(db, aid, {"label": "HQ", "company": "Acme Invoicing AB",
                                  "line1": "1 Rd", "city": "Goteborg", "is_invoice": 1})
    assert repo.get_address(db, aid)["city"] == "Goteborg"

    repo.delete_address(db, aid)
    assert repo.list_addresses(db, cid) == []


def test_default_flag_is_exclusive_per_usage(db):
    cid = repo.create_contact(db, {"kind": "customer", "name": "Acme"})
    a1 = repo.create_address(db, cid, {"label": "Plant1", "is_delivery": 1, "is_default_delivery": 1})
    a2 = repo.create_address(db, cid, {"label": "Plant2", "is_delivery": 1, "is_default_delivery": 1})
    # the newer default wins; the old one is demoted but still a usable delivery address
    assert repo.default_delivery_address(db, cid)["id"] == a2
    assert {x["id"] for x in repo.addresses_for(db, cid, "delivery")} == {a1, a2}
    assert repo.addresses_for(db, cid, "delivery")[0]["id"] == a2  # default first

    # switching the default via set_default_address clears the previous and marks usable
    repo.set_default_address(db, a1, "delivery")
    assert repo.default_delivery_address(db, cid)["id"] == a1
    # delivery and invoice defaults are independent
    inv = repo.create_address(db, cid, {"label": "Bill", "is_invoice": 1, "is_default_invoice": 1})
    assert repo.default_invoice_address(db, cid)["id"] == inv
    assert repo.default_delivery_address(db, cid)["id"] == a1


def test_addresses_cascade_on_contact_delete(db):
    cid = repo.create_contact(db, {"kind": "customer", "name": "Acme"})
    repo.create_address(db, cid, {"label": "X", "is_delivery": 1})
    with db.connect() as conn:
        conn.execute("DELETE FROM contacts WHERE id = ?", (cid,))
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM contact_addresses WHERE contact_id = ?", (cid,)).fetchone()[0] == 0
