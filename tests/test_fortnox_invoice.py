import pytest

from digisearch.fortnox import FortnoxError
from digisearch.web.core import FeatureRegistry
from digisearch.web.core.db import Database
from digisearch.web.features.catalog import feature as catalog_feature
from digisearch.web.features.catalog import repo as catrepo
from digisearch.web.features.contacts import feature as contacts_feature
from digisearch.web.features.contacts import repo as conrepo
from digisearch.web.features.customer_orders import feature as customer_orders_feature
from digisearch.web.features.customer_orders import repo as corepo
from digisearch.web.features.despatch import feature as despatch_feature
from digisearch.web.features.despatch import fortnox_invoice as fi
from digisearch.web.features.despatch import repo as despatch_repo
from digisearch.web.features.setup import feature as setup_feature


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "f.db")
    reg = FeatureRegistry()
    reg.register(catalog_feature, contacts_feature, customer_orders_feature,
                 despatch_feature, setup_feature)
    database.apply_migrations(reg)
    return database


class FakeFortnox:
    def __init__(self, *, existing_by_org=None, new_customer="500", invoice_no="2001"):
        self.created_customers = []
        self.created_invoices = []
        self._existing = existing_by_org or {}
        self._new_customer = new_customer
        self._invoice_no = invoice_no

    def find_customer_by_orgno(self, org):
        return self._existing.get((org or "").strip())

    def create_customer(self, payload):
        self.created_customers.append(payload)
        return {"CustomerNumber": self._new_customer}

    def create_invoice(self, payload):
        self.created_invoices.append(payload)
        return {"DocumentNumber": self._invoice_no}


def _despatch(db, *, org_no=None, qty=2, price=50.0):
    cust = conrepo.create_contact(db, {"kind": "customer", "name": "Acme AB",
                                       "org_no": org_no, "email": "a@acme.se"})
    pid = catrepo.create_part(db, part={"part_no": "99-1", "value": "Widget"},
                              supplier_lines=[], opening={"qty": 100})
    oid = corepo.create_order(db, {"customer_id": cust})
    corepo.add_line(db, oid, pid, qty, price, None)
    line_id = despatch_repo.shippable_lines(db, oid)[0]["line_id"]
    desp_id = despatch_repo.create_packing_list(db, oid, {line_id: qty})
    # pack every line, confirm ready, and dispatch -> status 'open' (despatched, ready to invoice)
    all_lines = {ln["id"] for ln in despatch_repo.get_despatch(db, desp_id)["lines"]}
    despatch_repo.set_packing(db, desp_id, all_lines)
    despatch_repo.confirm_packed(db, desp_id)
    despatch_repo.dispatch(db, desp_id)
    return cust, desp_id


def test_needs_customer_confirmation_creates_nothing(db):
    cust, desp = _despatch(db, org_no="556677-8899")
    fake = FakeFortnox()  # no existing customer for that org
    out = fi.invoice_despatch(db, desp, client=fake, confirm_customer=False)

    assert out["status"] == "needs_customer"
    assert "customer_preview" in out and out["customer_preview"]["Name"] == "Acme AB"
    assert fake.created_customers == [] and fake.created_invoices == []
    assert conrepo.get_contact(db, cust)["fortnox_customer_number"] is None
    d = despatch_repo.get_despatch(db, desp)
    assert d["invoice_no"] is None and "confirm" in (d["invoice_error"] or "").lower()


def test_confirm_creates_customer_links_and_invoices(db):
    cust, desp = _despatch(db, org_no="556677-8899")
    fake = FakeFortnox(new_customer="700", invoice_no="3001")
    out = fi.invoice_despatch(db, desp, client=fake, confirm_customer=True)

    assert out["status"] == "invoiced" and out["invoice_no"] == "3001"
    assert len(fake.created_customers) == 1 and len(fake.created_invoices) == 1
    assert conrepo.get_contact(db, cust)["fortnox_customer_number"] == "700"
    d = despatch_repo.get_despatch(db, desp)
    assert d["invoice_no"] == "3001" and d["status"] == "invoiced" and d["invoice_error"] is None


def test_existing_fortnox_customer_matched_by_org_no(db):
    cust, desp = _despatch(db, org_no="556677-8899")
    fake = FakeFortnox(existing_by_org={"556677-8899": {"CustomerNumber": "42"}})
    out = fi.invoice_despatch(db, desp, client=fake, confirm_customer=False)

    assert out["status"] == "invoiced"
    assert fake.created_customers == []                      # matched, not created
    assert conrepo.get_contact(db, cust)["fortnox_customer_number"] == "42"
    assert fake.created_invoices[0]["CustomerNumber"] == "42"


def test_already_linked_customer_invoices_directly(db):
    cust, desp = _despatch(db, org_no="556677-8899")
    with db.connect() as conn:
        conn.execute("UPDATE contacts SET fortnox_customer_number = '999' WHERE id = ?", (cust,))
        conn.commit()
    fake = FakeFortnox()
    out = fi.invoice_despatch(db, desp, client=fake, confirm_customer=False)
    assert out["status"] == "invoiced"
    assert fake.created_invoices[0]["CustomerNumber"] == "999"


def test_invoice_payload_is_free_text_with_default_vat(db):
    _, desp = _despatch(db, org_no="1", qty=3, price=65.0)
    with db.connect() as conn:  # pretend already linked so we go straight to invoicing
        conn.execute("UPDATE contacts SET fortnox_customer_number='1' WHERE org_no='1'")
        conn.commit()
    fake = FakeFortnox()
    fi.invoice_despatch(db, desp, client=fake)
    inv = fake.created_invoices[0]
    assert inv["VATIncluded"] is False
    row = inv["InvoiceRows"][0]
    assert "ArticleNumber" not in row                         # free-text row
    assert row["Description"].startswith("99-1") and row["DeliveredQuantity"] == 3
    assert row["Price"] == 65.0 and row["VAT"] == 25


def test_default_vat_and_account_from_settings(db):
    from digisearch.web.features.setup import repo as setuprepo
    setuprepo.save_fortnox(db, {"client_id": "c", "client_secret": "s",
                                "redirect_uri": "r", "default_vat": "6", "default_account": "3011"})
    _, desp = _despatch(db, org_no="1")
    with db.connect() as conn:
        conn.execute("UPDATE contacts SET fortnox_customer_number='1' WHERE org_no='1'")
        conn.commit()
    fake = FakeFortnox()
    fi.invoice_despatch(db, desp, client=fake)
    row = fake.created_invoices[0]["InvoiceRows"][0]
    assert row["VAT"] == 6 and row["AccountNumber"] == 3011


def test_api_error_is_captured_on_despatch(db):
    _, desp = _despatch(db, org_no="1")
    with db.connect() as conn:
        conn.execute("UPDATE contacts SET fortnox_customer_number='1' WHERE org_no='1'")
        conn.commit()

    class Boom(FakeFortnox):
        def create_invoice(self, payload):
            raise FortnoxError("Customer is blocked")

    out = fi.invoice_despatch(db, desp, client=Boom())
    assert out["status"] == "error" and "blocked" in out["message"]
    d = despatch_repo.get_despatch(db, desp)
    assert d["invoice_no"] is None and "blocked" in d["invoice_error"]


def test_not_connected_returns_error(db):
    _, desp = _despatch(db, org_no="1")
    out = fi.invoice_despatch(db, desp)            # no client, no config saved
    assert out["status"] == "error" and "connect" in out["message"].lower()


def test_already_invoiced_is_idempotent(db):
    _, desp = _despatch(db, org_no="1")
    with db.connect() as conn:
        conn.execute("UPDATE contacts SET fortnox_customer_number='1' WHERE org_no='1'")
        conn.commit()
    fake = FakeFortnox()
    fi.invoice_despatch(db, desp, client=fake)
    out = fi.invoice_despatch(db, desp, client=fake)    # second call
    assert out["status"] == "invoiced" and len(fake.created_invoices) == 1
