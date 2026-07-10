import pytest

from digisearch.web.core import FeatureRegistry
from digisearch.web.core.db import Database
from digisearch.web.features.catalog import feature as catalog_feature
from digisearch.web.features.contacts import feature as contacts_feature
from digisearch.web.features.contacts import importer, repo


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "c.db")
    reg = FeatureRegistry()
    # catalog first (as in production) so `suppliers` exists — contacts mirrors supplier-kind
    # contacts into it and the v4 backfill migration writes to it.
    reg.register(catalog_feature, contacts_feature)
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


def test_upsert_supplier_matches_and_never_wipes(db):
    from digisearch.web.features.catalog import repo as catrepo

    sid = catrepo.upsert_supplier(db, name="Acme Parts", short_name="ACM", currency="SEK",
                                  url="https://acme.example")
    assert [s["name"] for s in catrepo.suppliers(db)] == ["Acme Parts"]

    # Same name (case-insensitive) updates the existing row, doesn't duplicate.
    again = catrepo.upsert_supplier(db, name="acme parts", currency="USD")
    assert again == sid
    with db.connect() as conn:
        row = conn.execute("SELECT short_name, url, currency FROM suppliers WHERE id=?",
                           (sid,)).fetchone()
    # currency updated; blank short_name/url did NOT overwrite the earlier values (COALESCE).
    assert row["currency"] == "USD" and row["short_name"] == "ACM"
    assert row["url"] == "https://acme.example"


def test_upsert_supplier_matches_by_minimrp_id_across_rename(db):
    from digisearch.web.features.catalog import repo as catrepo

    sid = catrepo.upsert_supplier(db, name="Old Name", minimrp_id=42)
    # Same miniMRP id but a new name -> same row is updated (renamed), not a new one.
    again = catrepo.upsert_supplier(db, name="New Name", minimrp_id=42)
    assert again == sid
    assert [s["name"] for s in catrepo.suppliers(db)] == ["New Name"]


def test_backfill_migration_seeds_suppliers_from_supplier_contacts(db):
    """The v4 contacts migration bridges pre-existing supplier contacts into `suppliers`.
    Re-running its exact SQL is a no-op (idempotent) and ignores non-supplier contacts."""
    from digisearch.web.features.catalog import repo as catrepo

    repo.create_contact(db, {"kind": "supplier", "name": "Legacy Sup", "website": "https://x"})
    repo.create_contact(db, {"kind": "customer", "name": "Some Customer"})

    backfill = """
        INSERT INTO suppliers (name, short_name, url, currency, minimrp_id)
        SELECT c.name, c.short_name, c.website, c.currency, NULL
        FROM contacts c
        WHERE c.kind = 'supplier'
          AND NOT EXISTS (SELECT 1 FROM suppliers s WHERE lower(s.name) = lower(c.name))
        GROUP BY lower(c.name);
    """
    with db.connect() as conn:
        conn.executescript(backfill)
        conn.executescript(backfill)  # idempotent re-run
        conn.commit()

    names = [s["name"] for s in catrepo.suppliers(db)]
    assert names == ["Legacy Sup"]  # supplier seeded once; customer ignored


def test_addresses_cascade_on_contact_delete(db):
    cid = repo.create_contact(db, {"kind": "customer", "name": "Acme"})
    repo.create_address(db, cid, {"label": "X", "is_delivery": 1})
    with db.connect() as conn:
        conn.execute("DELETE FROM contacts WHERE id = ?", (cid,))
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM contact_addresses WHERE contact_id = ?", (cid,)).fetchone()[0] == 0
