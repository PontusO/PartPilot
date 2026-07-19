import pytest

from digisearch.web.core import FeatureRegistry
from digisearch.web.core.db import Database
from digisearch.web.features.assemblies import feature as assemblies_feature
from digisearch.web.features.assemblies import repo as asmrepo
from digisearch.web.features.catalog import feature as catalog_feature
from digisearch.web.features.catalog import repo as catrepo
from digisearch.web.features.contacts import feature as contacts_feature
from digisearch.web.features.contacts import repo as conrepo
from digisearch.web.features.customer_orders import feature as co_feature
from digisearch.web.features.customer_orders import repo
from digisearch.web.features.setup import feature as setup_feature


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "co.db")
    reg = FeatureRegistry()
    reg.register(catalog_feature, assemblies_feature, contacts_feature, co_feature, setup_feature)
    database.apply_migrations(reg)
    return database


def _seed(db):
    """A customer and a part whose cost (from its default supplier) is 2.0."""
    cust = conrepo.create_contact(db, {"kind": "customer", "name": "Acme AB"})
    part = catrepo.create_part(
        db, part={"part_no": "WIDGET-1", "value": "Blue widget"},
        supplier_lines=[{"supplier_name": "X", "unit_price": 2.0, "reel_qty": 1, "is_default": True}],
    )
    return cust, part


def test_create_order_and_summary(db):
    cust, _ = _seed(db)
    oid = repo.create_order(db, {"customer_id": cust, "order_ref": "SO-1",
                                 "status": "confirmed", "currency": "SEK"})
    o = repo.get_order(db, oid)
    assert o["customer_name"] == "Acme AB" and o["status"] == "confirmed"
    assert repo.summary(db) == {"total": 1, "open": 1, "backlog": 0}


def test_order_ref_auto_numbered_when_blank(db):
    cust, _ = _seed(db)
    oid = repo.create_order(db, {"customer_id": cust})
    assert repo.get_order(db, oid)["order_ref"] == f"CO-{oid:05d}"   # auto CO-NNNNN
    oid2 = repo.create_order(db, {"customer_id": cust, "order_ref": "SO-CUSTOM"})
    assert repo.get_order(db, oid2)["order_ref"] == "SO-CUSTOM"      # explicit ref preserved


def test_next_order_ref_and_rederive(db):
    cust, _ = _seed(db)
    assert repo.next_order_ref(db) == "CO-00001"                     # prediction for the form
    oid = repo.create_order(db, {"customer_id": cust})
    assert repo.next_order_ref(db) == f"CO-{oid + 1:05d}"

    # two stale forms both submitting the same predicted CO-ref bind to their real ids (no collision)
    a = repo.create_order(db, {"customer_id": cust, "order_ref": "CO-00001"})
    b = repo.create_order(db, {"customer_id": cust, "order_ref": "CO-00001"})
    assert repo.get_order(db, a)["order_ref"] == f"CO-{a:05d}"
    assert repo.get_order(db, b)["order_ref"] == f"CO-{b:05d}" and a != b


def test_default_status_is_draft(db):
    cust, _ = _seed(db)
    oid = repo.create_order(db, {"customer_id": cust})  # no status given
    assert repo.get_order(db, oid)["status"] == "draft"
    assert repo.summary(db)["open"] == 1  # NULL status would have broken this


def test_lines_default_price_and_totals(db):
    cust, part = _seed(db)
    oid = repo.create_order(db, {"customer_id": cust, "tax_rate": 25})
    # Loaded parts price = material 2.0 x overhead 1.30 = 2.6 (no mfg margin — priced outside)
    repo.add_line(db, oid, part, qty=10, unit_price=None, discount=None)
    ln = repo.get_order(db, oid)["lines"][0]
    assert ln["unit_price"] == pytest.approx(2.6) and ln["line_total"] == pytest.approx(26.0)
    assert ln["price_overridden"] is False
    assert repo.get_order(db, oid)["grand_total"] == pytest.approx(32.5)  # 26 + 25% tax

    repo.update_line(db, oid, ln["id"], qty=10, unit_price=5.0, discount=10)  # net 4.5 -> 45
    o = repo.get_order(db, oid)
    assert o["lines"][0]["price_overridden"] is True     # explicit price pins the line
    assert abs(o["lines"][0]["net_price"] - 4.5) < 1e-9
    assert abs(o["lines"][0]["line_total"] - 45.0) < 1e-9
    assert abs(o["grand_total"] - 56.25) < 1e-9  # 45 + 25% tax

    repo.delete_line(db, oid, ln["id"])
    assert repo.get_order(db, oid)["lines"] == []


def test_order_line_override_and_reprice(db):
    cust, part = _seed(db)   # cost 2.0
    # loaded cost tiers (the "sell tiers" table = internal loaded cost); the line prices at the tier
    catrepo.replace_sell_tiers(db, part, [{"break_qty": 1, "unit_price": 5.0},
                                          {"break_qty": 100, "unit_price": 3.0}])
    oid = repo.create_order(db, {"customer_id": cust})

    # Auto line at qty 10 -> below the 100 break -> loaded 5.0
    repo.add_line(db, oid, part, qty=10, unit_price=None, discount=None)
    ln = repo.get_order(db, oid)["lines"][0]
    assert ln["unit_price"] == pytest.approx(5.0) and ln["price_overridden"] is False

    # A blank-price update at qty 100 re-prices an un-overridden line to the 100 loaded tier (3.0)
    repo.update_line(db, oid, ln["id"], qty=100, unit_price=None, discount=None)
    ln = repo.get_order(db, oid)["lines"][0]
    assert ln["unit_price"] == pytest.approx(3.0) and ln["price_overridden"] is False

    # An explicit price overrides and survives a later qty-only change
    repo.update_line(db, oid, ln["id"], qty=100, unit_price=9.99, discount=None)
    repo.update_line(db, oid, ln["id"], qty=500, unit_price=9.99, discount=None)
    ln = repo.get_order(db, oid)["lines"][0]
    assert ln["unit_price"] == pytest.approx(9.99) and ln["price_overridden"] is True

    # Reprice recomputes at the ordered qty (500 -> loaded 3.0) and clears the override
    repo.reprice_line(db, oid, ln["id"])
    ln = repo.get_order(db, oid)["lines"][0]
    assert ln["unit_price"] == pytest.approx(3.0) and ln["price_overridden"] is False


def test_order_discount_delivery_and_backlog(db):
    cust, part = _seed(db)
    oid = repo.create_order(db, {"customer_id": cust, "discount_rate": 10,
                                 "delivery_charge": 5, "tax_rate": 0})
    repo.add_line(db, oid, part, qty=10, unit_price=10.0, discount=None)  # subtotal 100
    o = repo.get_order(db, oid)
    assert o["subtotal"] == 100.0 and abs(o["discount_amount"] - 10.0) < 1e-9
    assert abs(o["grand_total"] - 95.0) < 1e-9  # 100 - 10 discount + 5 delivery
    # backlog = goods subtotal of open orders (before order-level discount/tax)
    assert repo.summary(db)["backlog"] == 100.0


def test_list_filter_and_search(db):
    cust, _ = _seed(db)
    repo.create_order(db, {"customer_id": cust, "order_ref": "SO-100", "status": "draft"})
    done = repo.create_order(db, {"customer_id": cust, "order_ref": "SO-200"})
    with db.connect() as conn:  # 'complete' is action-owned (invoicing) — set directly for the filter test
        conn.execute("UPDATE customer_orders SET status = 'complete' WHERE id = ?", (done,))
        conn.commit()
    assert len(repo.list_orders(db)) == 2
    assert len(repo.list_orders(db, status="draft")) == 1
    hits = repo.list_orders(db, search="SO-200")
    assert len(hits) == 1 and hits[0]["order_ref"] == "SO-200"
    assert repo.summary(db) == {"total": 2, "open": 1, "backlog": 0}


def test_assembly_line_defaults_to_loaded_cost(db):
    cust, comp = _seed(db)  # WIDGET-1 component, material cost 2.0
    asm = asmrepo.create_assembly(db, {"part_no": "ASSY-1"})  # ASSY unit_cost is NULL
    asmrepo.add_bom_line(db, asm, comp, 3, None)              # 3x the component
    oid = repo.create_order(db, {"customer_id": cust})
    repo.add_line(db, oid, asm, qty=2, unit_price=None, discount=None)
    ln = repo.get_order(db, oid)["lines"][0]
    # loaded build cost: leaf 2.0 x overhead 1.30 = 2.6, x3 = 7.8 (no mfg margin)
    assert ln["unit_price"] == pytest.approx(7.8)
    assert ln["line_total"] == pytest.approx(15.6)


def test_picker_prices_show_loaded_cost(db):
    _, comp = _seed(db)   # WIDGET-1 material cost 2.0
    asm = asmrepo.create_assembly(db, {"part_no": "ASSY-1"})
    asmrepo.add_bom_line(db, asm, comp, 3, None)
    picker = {p["part_no"]: p for p in repo.parts_for_picker(db)}
    # The pick hint is the loaded cost = what the order line will default to.
    assert picker["WIDGET-1"]["price"] == pytest.approx(2.6)   # 2.0 × overhead 1.30
    assert picker["ASSY-1"]["price"] == pytest.approx(7.8)     # 3×2.0 loaded 7.8


def test_allocate_and_release(db):
    from digisearch.web.features.catalog import stock as cstock

    cust, part = _seed(db)
    cstock.adjust_stock(db, part, delta=30, mtype=cstock.RECEIVE)  # 30 on hand
    oid = repo.create_order(db, {"customer_id": cust})
    repo.add_line(db, oid, part, 20, 5.0, None)

    assert repo.allocate_order(db, oid) == 20            # reserves the full ordered qty
    assert repo.get_order(db, oid)["lines"][0]["allocated"] == 20
    assert catrepo.get_part(db, part)["total_alloc"] == 20
    assert catrepo.get_part(db, part)["free"] == 10      # 30 − 20 reserved
    assert repo.allocate_order(db, oid) == 0             # already fully allocated, re-run is a no-op

    repo.release_order_allocations(db, oid)
    assert repo.get_order(db, oid)["lines"][0]["allocated"] == 0
    assert catrepo.get_part(db, part)["total_alloc"] == 0


def test_cancel_order_releases_allocations(db):
    from digisearch.web.features.catalog import stock as cstock

    cust, part = _seed(db)
    cstock.adjust_stock(db, part, delta=30, mtype=cstock.RECEIVE)
    oid = repo.create_order(db, {"customer_id": cust, "status": "confirmed"})
    repo.add_line(db, oid, part, 20, 5.0, None)
    repo.allocate_order(db, oid)
    assert catrepo.get_part(db, part)["total_alloc"] == 20

    repo.cancel_order(db, oid)
    assert repo.get_order(db, oid)["status"] == "cancelled"
    assert catrepo.get_part(db, part)["total_alloc"] == 0        # reserved stock rolled back
    assert repo.get_order(db, oid)["lines"][0]["allocated"] == 0


def test_cannot_cancel_shipped_order(db):
    cust, _ = _seed(db)
    oid = repo.create_order(db, {"customer_id": cust})
    with db.connect() as conn:  # 'shipped' is action-owned (dispatch) — set directly for the guard test
        conn.execute("UPDATE customer_orders SET status = 'shipped' WHERE id = ?", (oid,))
        conn.commit()
    with pytest.raises(ValueError):
        repo.cancel_order(db, oid)


def test_allocate_limited_by_free_stock(db):
    from digisearch.web.features.catalog import stock as cstock

    cust, part = _seed(db)
    cstock.adjust_stock(db, part, delta=8, mtype=cstock.RECEIVE)  # only 8 in stock
    oid = repo.create_order(db, {"customer_id": cust})
    repo.add_line(db, oid, part, 20, 5.0, None)
    assert repo.allocate_order(db, oid) == 8             # capped at free stock
    assert catrepo.get_part(db, part)["free"] == 0


def test_add_line_rejects_unknown_part(db):
    cust, _ = _seed(db)
    oid = repo.create_order(db, {"customer_id": cust})
    with pytest.raises(ValueError):
        repo.add_line(db, oid, 9999, qty=1, unit_price=None, discount=None)


def test_customers_picker_lists_only_customers(db):
    conrepo.create_contact(db, {"kind": "customer", "name": "Acme AB"})
    conrepo.create_contact(db, {"kind": "supplier", "name": "Digikey"})
    names = [c["name"] for c in repo.customers(db)]
    assert names == ["Acme AB"]


def test_acknowledge_stores_pdf_and_confirms(db):
    cust, part = _seed(db)
    oid = repo.create_order(db, {"customer_id": cust})        # draft
    repo.add_line(db, oid, part, qty=3, unit_price=10.0, discount=None)

    doc_id = repo.acknowledge_order(db, oid, user="anna")
    assert isinstance(doc_id, int)
    assert repo.get_order(db, oid)["status"] == "confirmed"   # draft advanced

    doc = repo.get_document(db, oid, "pdf")
    assert doc["content"][:5] == b"%PDF-" and doc["filename"] == f"OA-CO-{oid:05d}.pdf"
    docs = repo.documents_for_order(db, oid)
    assert len(docs) == 1 and docs[0]["created_by"] == "anna" and docs[0]["byte_size"] > 0

    # re-issuing after an amendment appends a new immutable version; both are kept
    repo.update_line(db, oid, repo.get_order(db, oid)["lines"][0]["id"], qty=5,
                     unit_price=10.0, discount=None)
    repo.acknowledge_order(db, oid, user="anna")
    after = repo.documents_for_order(db, oid)
    assert len(after) == 2 and after[0]["id"] > after[1]["id"]   # newest first
    assert repo.get_order(db, oid)["status"] == "confirmed"      # already confirmed, unchanged


def test_order_defaults_and_resolves_addresses(db):
    from digisearch.web.features.contacts import repo as conrepo

    cust, _ = _seed(db)
    inv = conrepo.create_address(db, cust, {"company": "Acme Billing AB", "line1": "1 Rd",
                                            "is_invoice": 1, "is_default_invoice": 1})
    dlv = conrepo.create_address(db, cust, {"line1": "2 St", "city": "Malmo",
                                            "is_delivery": 1, "is_default_delivery": 1})

    o = repo.get_order(db, repo.create_order(db, {"customer_id": cust}))
    assert o["invoice_address_id"] == inv and o["delivery_address_id"] == dlv  # defaulted from customer
    assert o["invoice_address"]["company"] == "Acme Billing AB"
    assert o["delivery_address"]["city"] == "Malmo"

    # explicit override at create time
    o2 = repo.get_order(db, repo.create_order(db, {"customer_id": cust, "invoice_address_id": dlv}))
    assert o2["invoice_address_id"] == dlv

    # update can change/clear the chosen addresses
    repo.update_order(db, o["id"], {"customer_id": cust, "status": "draft",
                                    "invoice_address_id": inv, "delivery_address_id": None})
    o3 = repo.get_order(db, o["id"])
    assert o3["invoice_address_id"] == inv and o3["delivery_address"] is None


def test_acknowledgement_pdf_uses_invoice_and_delivery(db):
    from digisearch.web.features.contacts import repo as conrepo
    from digisearch.web.features.customer_orders import export

    cust, part = _seed(db)
    conrepo.create_address(db, cust, {"company": "Acme Billing AB", "line1": "1 Rd",
                                      "country": "Sweden", "is_invoice": 1, "is_default_invoice": 1})
    conrepo.create_address(db, cust, {"company": "Acme Plant AB", "line1": "2 St",
                                      "country": "Norway", "is_delivery": 1, "is_default_delivery": 1})
    oid = repo.create_order(db, {"customer_id": cust})
    repo.add_line(db, oid, part, 3, 10.0, None)
    o = repo.get_order(db, oid)

    # each block draws on its own structured address (different company names + countries)
    assert o["invoice_address"]["company"] == "Acme Billing AB"
    assert o["delivery_address"]["company"] == "Acme Plant AB"
    assert export.ack_pdf(o, {"name": "Us AB"})[:5] == b"%PDF-"


def test_acknowledge_guards(db):
    cust, part = _seed(db)
    empty = repo.create_order(db, {"customer_id": cust})
    with pytest.raises(ValueError):                              # no lines
        repo.acknowledge_order(db, empty)

    shipped = repo.create_order(db, {"customer_id": cust})
    repo.add_line(db, shipped, part, qty=1, unit_price=1.0, discount=None)
    with db.connect() as conn:  # 'shipped' is action-owned (dispatch) — set directly for the guard test
        conn.execute("UPDATE customer_orders SET status = 'shipped' WHERE id = ?", (shipped,))
        conn.commit()
    with pytest.raises(ValueError):                              # not an open/unshipped status
        repo.acknowledge_order(db, shipped)
