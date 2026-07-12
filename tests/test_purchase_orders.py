import pytest

from digisearch.web.core import FeatureRegistry
from digisearch.web.core.db import Database
from digisearch.web.features.assemblies import feature as assemblies_feature
from digisearch.web.features.assemblies import repo as asmrepo
from digisearch.web.features.catalog import feature as catalog_feature
from digisearch.web.features.catalog import repo as catrepo
from digisearch.web.features.catalog import stock
from digisearch.web.features.contacts import feature as contacts_feature
from digisearch.web.features.customer_orders import feature as co_feature
from digisearch.web.features.purchase_orders import feature as po_feature
from digisearch.web.features.purchase_orders import repo
from digisearch.web.features.work_orders import feature as wo_feature
from digisearch.web.features.work_orders import repo as worepo


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "po.db")
    reg = FeatureRegistry()
    reg.register(catalog_feature, assemblies_feature, contacts_feature, co_feature,
                 wo_feature, po_feature)
    database.apply_migrations(reg)
    return database


def _setup_shortage(db):
    """R-1 (supplier Acme, 10 in stock); PROD-1 uses 2×R-1; an allocated WO for 20 → needs 40."""
    p = catrepo.create_part(
        db, part={"part_no": "R-1"},
        supplier_lines=[{"supplier_name": "Acme Supplies", "supplier_pno": "ACME-R1",
                         "unit_price": 0.5, "reel_qty": 1, "is_default": True}],
        opening={"qty": 10})
    asm = asmrepo.create_assembly(db, {"part_no": "PROD-1"})
    asmrepo.add_bom_line(db, asm, p, 2, None)
    wo = worepo.create_work_order(db, {"assembly_id": asm, "qty": 20})  # needs 40 of R-1
    return p, asm, wo


def test_shortage_suggestions(db):
    p, asm, wo = _setup_shortage(db)
    sugg = repo.shortage_suggestions(db)
    assert len(sugg) == 1
    s = sugg[0]
    assert s["part_id"] == p and s["required"] == 40 and s["free"] == 10 and s["short"] == 30
    assert s["supplier_name"] == "Acme Supplies" and s["supplier_pno"] == "ACME-R1"
    assert s["suggested_qty"] == 30 and abs(s["unit_price"] - 0.5) < 1e-9


def test_shortage_suggestions_grouped_by_supplier(db):
    p1 = catrepo.create_part(
        db, part={"part_no": "G-1"},
        supplier_lines=[{"supplier_name": "Acme Supplies", "supplier_pno": "A-1", "unit_price": 0.5,
                         "reel_qty": 1, "is_default": True}], opening={"qty": 10})
    p2 = catrepo.create_part(
        db, part={"part_no": "G-2"},
        supplier_lines=[{"supplier_name": "Beta Ltd", "supplier_pno": "B-1", "unit_price": 0.3,
                         "reel_qty": 1, "is_default": True}], opening={"qty": 0})
    asm = asmrepo.create_assembly(db, {"part_no": "GPROD"})
    asmrepo.add_bom_line(db, asm, p1, 2, None)
    asmrepo.add_bom_line(db, asm, p2, 1, None)
    worepo.create_work_order(db, {"assembly_id": asm, "qty": 20})  # both short

    groups = repo.shortage_suggestions_grouped(db)
    by_name = {g["supplier_name"]: g for g in groups}
    assert set(by_name) == {"Acme Supplies", "Beta Ltd"}
    assert [ln["part_no"] for ln in by_name["Acme Supplies"]["lines"]] == ["G-1"]
    assert [ln["part_no"] for ln in by_name["Beta Ltd"]["lines"]] == ["G-2"]
    for g in groups:  # every line sits under its own supplier
        assert all(ln["supplier_name"] == g["supplier_name"] for ln in g["lines"])


def test_create_pos_from_suggestions_and_on_order_clears_shortage(db):
    p, asm, wo = _setup_shortage(db)
    created = repo.create_pos_from_suggestions(db, {p: 30}, "u")
    assert len(created) == 1
    po = repo.get_po(db, created[0])
    assert po["status"] == "draft" and po["supplier_name"] == "Acme Supplies"
    assert po["lines"][0]["part_id"] == p and po["lines"][0]["qty"] == 30
    assert po["lines"][0]["supplier_pno"] == "ACME-R1"

    assert repo.shortage_suggestions(db)  # draft PO doesn't count as on-order yet
    repo.mark_ordered(db, created[0])
    assert repo.shortage_suggestions(db) == []  # 30 on order now covers the shortfall


def test_receive_po_moves_stock(db):
    p, asm, wo = _setup_shortage(db)
    po_id = repo.create_pos_from_suggestions(db, {p: 30}, "u")[0]
    repo.mark_ordered(db, po_id)
    line_id = repo.get_po(db, po_id)["lines"][0]["id"]

    repo.receive_po(db, po_id, {line_id: 20}, "u")          # partial
    assert catrepo.get_part(db, p)["total_qty"] == 30        # 10 + 20
    assert repo.get_po(db, po_id)["status"] == "ordered"
    assert repo.get_po(db, po_id)["lines"][0]["qty_received"] == 20

    repo.receive_po(db, po_id, {line_id: 10}, "u")          # the rest
    assert catrepo.get_part(db, p)["total_qty"] == 40
    assert repo.get_po(db, po_id)["status"] == "received"
    assert sum(1 for m in stock.movements_for_part(db, p) if m["mtype"] == "RECEIVE") == 2


def test_receive_creates_goods_receipt(db):
    p, asm, wo = _setup_shortage(db)
    po_id = repo.create_pos_from_suggestions(db, {p: 30}, "u")[0]
    repo.mark_ordered(db, po_id)
    line_id = repo.get_po(db, po_id)["lines"][0]["id"]

    grn_id = repo.receive_po(db, po_id, {line_id: 30}, "u", advice_no="ADV-7")
    assert grn_id is not None
    receipts = repo.receipts_for_po(db, po_id)
    assert len(receipts) == 1 and receipts[0]["advice_no"] == "ADV-7"

    from digisearch.web.features.goods_receipts import repo as grnrepo
    g = grnrepo.get_receipt(db, grn_id)
    assert g["po_no"] == f"PO-{po_id:05d}" and g["lines"][0]["qty"] == 30


def _acme_id(db):
    return next(s["id"] for s in repo.suppliers(db) if s["name"] == "Acme Supplies")


def test_po_line_priced_from_stored_cost_tier(db):
    """A non-distributor offer with stored cost tiers prices the PO line at the tier for the buy qty
    (no network)."""
    p = catrepo.create_part(db, part={"part_no": "T-1"}, supplier_lines=[{
        "supplier_name": "Acme Supplies", "supplier_pno": "A-1", "unit_price": 0.10, "reel_qty": 1,
        "is_default": True,
        "cost_tiers": [{"break_qty": 1, "unit_price": 0.10}, {"break_qty": 1000, "unit_price": 0.03}],
    }])
    po_id = repo.create_pos_from_suggestions(db, {p: 1000}, "u")[0]
    line = repo.get_po(db, po_id)["lines"][0]
    assert line["qty"] == 1000 and line["unit_price"] == pytest.approx(0.03)   # tier at 1000


def test_po_generation_refreshes_tiers_and_prices_live(db, monkeypatch):
    """A distributor offer is re-queried at PO generation: cost tiers refreshed, line priced from the
    tier at the buy qty, and the offer's flat price updated."""
    p = catrepo.create_part(db, part={"part_no": "DK-1"}, supplier_lines=[{
        "supplier_name": "Digi-Key", "supplier_pno": "DK-1-ND", "unit_price": 0.5, "reel_qty": 1,
        "is_default": True}])

    def fake_fetch(clients, name, pno):
        assert pno == "DK-1-ND"
        return ([{"break_qty": 1, "unit_price": 0.10}, {"break_qty": 100, "unit_price": 0.06},
                 {"break_qty": 1000, "unit_price": 0.03}], [])

    monkeypatch.setattr(repo.cost_refresh, "fetch_offer_breaks", fake_fetch)
    po_id = repo.create_pos_from_suggestions(db, {p: 500}, "u")[0]
    assert repo.get_po(db, po_id)["lines"][0]["unit_price"] == pytest.approx(0.06)  # 500 -> 100-break

    part = catrepo.get_part(db, p)
    cut = sorted((t["break_qty"], t["unit_price"]) for t in part["suppliers"][0]["cost_tiers"]
                 if t["kind"] == "cut")
    assert cut == [(1, 0.10), (100, 0.06), (1000, 0.03)]         # cost tiers refreshed from distributor
    assert part["suppliers"][0]["unit_price"] == pytest.approx(0.06)   # flat price updated to the tier


def test_reel_only_response_preserves_cut_tiers(db, monkeypatch):
    """A distributor reply with only reel breaks (no cut ladder) must not wipe the offer's stored
    cut cost tiers, and the line prices from the surviving cut tiers."""
    p = catrepo.create_part(db, part={"part_no": "RO-1"}, supplier_lines=[{
        "supplier_name": "Digi-Key", "supplier_pno": "RO-1-ND", "unit_price": 0.5, "reel_qty": 1,
        "is_default": True,
        "cost_tiers": [{"break_qty": 1, "unit_price": 0.10}, {"break_qty": 1000, "unit_price": 0.03}]}])
    # cut = [] (reel-only) -> _priced_line must fall through, not overwrite
    monkeypatch.setattr(repo.cost_refresh, "fetch_offer_breaks",
                        lambda *a: ([], [{"break_qty": 5000, "unit_price": 0.02}]))
    po_id = repo.create_pos_from_suggestions(db, {p: 1000}, "u")[0]
    assert repo.get_po(db, po_id)["lines"][0]["unit_price"] == pytest.approx(0.03)  # stored cut tier

    cut = sorted((t["break_qty"], t["unit_price"]) for t in catrepo.get_part(db, p)["suppliers"][0]["cost_tiers"]
                 if t["kind"] == "cut")
    assert cut == [(1, 0.10), (1000, 0.03)]     # original cut tiers intact (not wiped by reel-only)


def test_po_generation_falls_back_when_lookup_fails(db, monkeypatch):
    """If the distributor can't be queried, pricing falls back to the flat unit price (no tiers)."""
    p = catrepo.create_part(db, part={"part_no": "DK-2"}, supplier_lines=[{
        "supplier_name": "Digi-Key", "supplier_pno": "DK-2-ND", "unit_price": 0.5, "reel_qty": 1,
        "is_default": True}])
    monkeypatch.setattr(repo.cost_refresh, "fetch_offer_breaks", lambda *a: None)
    po_id = repo.create_pos_from_suggestions(db, {p: 1000}, "u")[0]
    assert repo.get_po(db, po_id)["lines"][0]["unit_price"] == pytest.approx(0.5)   # flat fallback


def test_add_line_explicit_price_vs_tiered(db):
    p = catrepo.create_part(db, part={"part_no": "AL-1"}, supplier_lines=[{
        "supplier_name": "Acme Supplies", "supplier_pno": "A", "unit_price": 0.10, "reel_qty": 1,
        "is_default": True,
        "cost_tiers": [{"break_qty": 1, "unit_price": 0.10}, {"break_qty": 1000, "unit_price": 0.03}]}])
    po = repo.create_po(db, {"supplier_id": _acme_id(db)})
    repo.add_line(db, po, p, 1000, None)     # no price -> stored tier at 1000
    repo.add_line(db, po, p, 1000, 0.99)     # explicit -> verbatim
    lines = repo.get_po(db, po)["lines"]
    assert lines[0]["unit_price"] == pytest.approx(0.03)
    assert lines[1]["unit_price"] == pytest.approx(0.99)


def test_shortage_suggestion_previews_tier_price(db):
    p = catrepo.create_part(db, part={"part_no": "SP-1"}, supplier_lines=[{
        "supplier_name": "Acme Supplies", "supplier_pno": "A", "unit_price": 0.10, "reel_qty": 1,
        "is_default": True,
        "cost_tiers": [{"break_qty": 1, "unit_price": 0.10}, {"break_qty": 50, "unit_price": 0.04}]}],
        opening={"qty": 0})
    asm = asmrepo.create_assembly(db, {"part_no": "SPA"})
    asmrepo.add_bom_line(db, asm, p, 1, None)
    worepo.create_work_order(db, {"assembly_id": asm, "qty": 100})   # short 100
    s = repo.shortage_suggestions(db)[0]
    assert s["short"] == 100 and s["unit_price"] == pytest.approx(0.04)   # stored tier at 100


def test_po_generation_recalcs_sell_tiers(db):
    """Generating a PO re-anchors the part's sell tiers to the ordered (list) price x markup."""
    p = catrepo.create_part(db, part={"part_no": "GS-1"}, supplier_lines=[{
        "supplier_name": "Acme Supplies", "supplier_pno": "A", "unit_price": 2.0, "reel_qty": 1,
        "is_default": True,
        "cost_tiers": [{"break_qty": 1, "unit_price": 2.00}, {"break_qty": 1000, "unit_price": 1.00}]}])
    repo.create_pos_from_suggestions(db, {p: 1000}, "u")     # anchor = stored tier at 1000 = 1.00
    sell = {t["break_qty"]: t["unit_price"] for t in catrepo.get_part(db, p)["sell_tiers"]}
    assert sell == pytest.approx({1: 2.60, 1000: 1.30})      # list x default markup 1.30


def test_negotiated_line_price_rebases_sell_tiers(db):
    p = catrepo.create_part(db, part={"part_no": "GS-2"}, supplier_lines=[{
        "supplier_name": "Acme Supplies", "supplier_pno": "A", "unit_price": 2.0, "reel_qty": 1,
        "is_default": True,
        "cost_tiers": [{"break_qty": 1, "unit_price": 2.00}, {"break_qty": 1000, "unit_price": 1.00}]}])
    po = repo.create_po(db, {"supplier_id": _acme_id(db)})
    repo.add_line(db, po, p, 1000, 0.90)                     # negotiated 0.90 for the 1000 tier
    sell = {t["break_qty"]: t["unit_price"] for t in catrepo.get_part(db, p)["sell_tiers"]}
    assert sell == pytest.approx({1: 2.34, 1000: 1.17})     # 0.90 x (cost/1.00) x 1.30

    line_id = repo.get_po(db, po)["lines"][0]["id"]
    repo.update_line(db, po, line_id, 1000, 0.80)            # re-negotiate -> re-anchor
    sell = {t["break_qty"]: t["unit_price"] for t in catrepo.get_part(db, p)["sell_tiers"]}
    assert sell == pytest.approx({1: 2.08, 1000: 1.04})     # 0.80 x (cost/1.00) x 1.30


def test_receive_records_last_purchase_price(db):
    """Receiving overwrites the matched supplier offer's unit price with the price paid, records it
    on the GRN line, and leaves parts.unit_cost untouched."""
    p, asm, wo = _setup_shortage(db)          # R-1 default supplier Acme @ 0.5/pc, unit_cost 0.5
    po_id = repo.create_pos_from_suggestions(db, {p: 30}, "u")[0]
    line_id = repo.get_po(db, po_id)["lines"][0]["id"]
    repo.update_line(db, po_id, line_id, 30, 0.30)   # negotiated a lower price on the draft
    repo.mark_ordered(db, po_id)
    grn_id = repo.receive_po(db, po_id, {line_id: 30}, "u")

    part = catrepo.get_part(db, p)
    acme = next(s for s in part["suppliers"] if s["supplier_name"] == "Acme Supplies")
    assert acme["unit_price"] == pytest.approx(0.30)   # offer price = last purchase price
    assert part["unit_cost"] == pytest.approx(0.5)     # unit_cost deliberately unchanged

    from digisearch.web.features.goods_receipts import repo as grnrepo
    g = grnrepo.get_receipt(db, grn_id)
    assert g["lines"][0]["unit_price"] == pytest.approx(0.30)   # per-receipt history
    assert g["lines"][0]["line_value"] == pytest.approx(9.0)    # 30 * 0.30
    assert g["total_value"] == pytest.approx(9.0)


def test_second_purchase_overwrites_offer_but_keeps_history(db):
    p, asm, wo = _setup_shortage(db)
    po1 = repo.create_pos_from_suggestions(db, {p: 30}, "u")[0]
    l1 = repo.get_po(db, po1)["lines"][0]["id"]
    repo.update_line(db, po1, l1, 30, 0.30)
    repo.mark_ordered(db, po1)
    grn1 = repo.receive_po(db, po1, {l1: 30}, "u")

    po2 = repo.create_po(db, {"supplier_id": _acme_id(db)})
    repo.add_line(db, po2, p, 10, 0.20)              # a later, cheaper buy
    repo.mark_ordered(db, po2)
    l2 = repo.get_po(db, po2)["lines"][0]["id"]
    repo.receive_po(db, po2, {l2: 10}, "u")

    acme = next(s for s in catrepo.get_part(db, p)["suppliers"]
                if s["supplier_name"] == "Acme Supplies")
    assert acme["unit_price"] == pytest.approx(0.20)   # latest purchase wins

    from digisearch.web.features.goods_receipts import repo as grnrepo
    assert grnrepo.get_receipt(db, grn1)["lines"][0]["unit_price"] == pytest.approx(0.30)  # history intact


def test_receive_no_offer_for_supplier_skips_price(db):
    """A part bought from a supplier that isn't one of its offers: stock + GRN line recorded, but no
    offer price is written (and none fabricated)."""
    p = catrepo.create_part(
        db, part={"part_no": "NB-1"},
        supplier_lines=[{"supplier_name": "Acme Supplies", "supplier_pno": "A", "unit_price": 0.5,
                         "reel_qty": 1, "is_default": True}], opening={"qty": 0})
    beta_id = catrepo.upsert_supplier(db, name="Beta Ltd")
    po = repo.create_po(db, {"supplier_id": beta_id})
    repo.add_line(db, po, p, 5, 0.90)
    repo.mark_ordered(db, po)
    line_id = repo.get_po(db, po)["lines"][0]["id"]
    grn_id = repo.receive_po(db, po, {line_id: 5}, "u")

    part = catrepo.get_part(db, p)
    assert len(part["suppliers"]) == 1                        # no Beta offer was fabricated
    assert part["suppliers"][0]["unit_price"] == pytest.approx(0.5)   # Acme offer untouched
    assert part["total_qty"] == 5                            # stock still moved
    from digisearch.web.features.goods_receipts import repo as grnrepo
    assert grnrepo.get_receipt(db, grn_id)["lines"][0]["unit_price"] == pytest.approx(0.90)


def test_receive_updates_only_the_receiving_offer(db):
    """With two offers, receiving against the non-default one updates only that offer; the default
    offer (and thus parts.unit_cost) is untouched."""
    p = catrepo.create_part(
        db, part={"part_no": "TWO-1"}, supplier_lines=[
            {"supplier_name": "Acme Supplies", "supplier_pno": "A", "unit_price": 0.5,
             "reel_qty": 1, "is_default": True},
            {"supplier_name": "Beta Ltd", "supplier_pno": "B", "unit_price": 0.4,
             "reel_qty": 1, "is_default": False}], opening={"qty": 0})
    beta_id = next(s["id"] for s in repo.suppliers(db) if s["name"] == "Beta Ltd")
    po = repo.create_po(db, {"supplier_id": beta_id})
    repo.add_line(db, po, p, 10, 0.35)
    repo.mark_ordered(db, po)
    line_id = repo.get_po(db, po)["lines"][0]["id"]
    repo.receive_po(db, po, {line_id: 10}, "u")

    part = catrepo.get_part(db, p)
    by = {s["supplier_name"]: s for s in part["suppliers"]}
    assert by["Beta Ltd"]["unit_price"] == pytest.approx(0.35)   # the receiving offer updated
    assert by["Acme Supplies"]["unit_price"] == pytest.approx(0.5)  # default offer untouched
    assert part["unit_cost"] == pytest.approx(0.5)              # unit_cost (from default) untouched


def test_manual_po_create_and_lines(db):
    p = catrepo.create_part(db, part={"part_no": "M-1"},
                            supplier_lines=[{"supplier_name": "Sup", "unit_price": 2.0,
                                             "reel_qty": 1, "is_default": True}])
    sup_id = repo.suppliers(db)[0]["id"]
    po_id = repo.create_po(db, {"supplier_id": sup_id})
    repo.add_line(db, po_id, p, 100, None)  # price defaults to the part's cost
    po = repo.get_po(db, po_id)
    assert po["po_no"] == f"PO-{po_id:05d}" and po["lines"][0]["qty"] == 100
    assert po["lines"][0]["unit_price"] == 2.0

    line_id = po["lines"][0]["id"]
    repo.update_line(db, po_id, line_id, 250, 3.0)     # change qty + unit price before placing
    edited = repo.get_po(db, po_id)["lines"][0]
    assert edited["qty"] == 250 and edited["unit_price"] == 3.0

    repo.delete_line(db, po_id, line_id)
    assert repo.get_po(db, po_id)["lines"] == []


def test_cannot_change_line_after_placing(db):
    p, asm, wo = _setup_shortage(db)
    po_id = repo.create_pos_from_suggestions(db, {p: 30}, "u")[0]
    line_id = repo.get_po(db, po_id)["lines"][0]["id"]
    repo.mark_ordered(db, po_id)  # placed → lines frozen
    with pytest.raises(ValueError):
        repo.update_line(db, po_id, line_id, 99, 1.0)
    assert repo.get_po(db, po_id)["lines"][0]["qty"] == 30


def test_po_csv_and_pdf_export(db):
    from digisearch.web.features.purchase_orders import export

    p = catrepo.create_part(
        db, part={"part_no": "EXP-1", "mfr_pno": "MFR-1", "description": "a blue widget"},
        supplier_lines=[{"supplier_name": "VendorX", "supplier_pno": "VX-9", "unit_price": 1.5,
                         "reel_qty": 1, "is_default": True}])
    sup_id = repo.suppliers(db)[0]["id"]
    po_id = repo.create_po(db, {"supplier_id": sup_id, "currency": "SEK"})
    repo.add_line(db, po_id, p, 100, None)
    po = repo.get_po(db, po_id)

    text = export.po_csv(po)
    assert text.splitlines()[0] == "Supplier Part No,Manufacturer Part No,Quantity,Description,Unit Price,Reference"
    assert "VX-9" in text and "MFR-1" in text and "100" in text and "EXP-1" in text

    pdf = export.po_pdf(po, {"name": "Acme Co", "city": "Gothenburg"})
    assert pdf[:5] == b"%PDF-" and len(pdf) > 500


def test_placing_po_archives_documents(db):
    p, asm, wo = _setup_shortage(db)
    po_id = repo.create_pos_from_suggestions(db, {p: 30}, "u")[0]
    assert repo.documents_for_po(db, po_id) == []   # nothing archived while draft

    repo.mark_ordered(db, po_id, "boss")
    docs = repo.documents_for_po(db, po_id)
    assert {d["kind"] for d in docs} == {"csv", "pdf"}
    assert all(d["placed_by"] == "boss" and d["byte_size"] > 0 for d in docs)

    assert b"Supplier Part No" in repo.get_document(db, po_id, "csv")["content"]
    assert repo.get_document(db, po_id, "pdf")["content"][:5] == b"%PDF-"


def test_delete_unfulfilled_po_purges_documents(db):
    # a placed PO that never received goods (supplier cancelled / lost in transit)
    p, asm, wo = _setup_shortage(db)
    po_id = repo.create_pos_from_suggestions(db, {p: 30}, "u")[0]
    repo.mark_ordered(db, po_id, "boss")            # placed → archives CSV + PDF
    assert len(repo.documents_for_po(db, po_id)) == 2

    repo.delete_po(db, po_id)
    assert repo.get_po(db, po_id) is None
    assert repo.documents_for_po(db, po_id) == []   # archived docs purged by cascade


def test_delete_draft_po(db):
    p = catrepo.create_part(db, part={"part_no": "DEL-1"},
                            supplier_lines=[{"supplier_name": "S", "unit_price": 1.0,
                                             "reel_qty": 1, "is_default": True}])
    sup_id = repo.suppliers(db)[0]["id"]
    po_id = repo.create_po(db, {"supplier_id": sup_id})
    repo.add_line(db, po_id, p, 5, None)
    repo.delete_po(db, po_id)
    assert repo.get_po(db, po_id) is None


def test_cannot_delete_po_with_received_goods(db):
    p, asm, wo = _setup_shortage(db)
    po_id = repo.create_pos_from_suggestions(db, {p: 30}, "u")[0]
    repo.mark_ordered(db, po_id)
    line_id = repo.get_po(db, po_id)["lines"][0]["id"]
    repo.receive_po(db, po_id, {line_id: 30}, "u")  # goods in → a GRN now exists
    with pytest.raises(ValueError):
        repo.delete_po(db, po_id)
    assert repo.get_po(db, po_id) is not None        # retained


def test_cannot_receive_a_draft_then_place_first(db):
    p, asm, wo = _setup_shortage(db)
    po_id = repo.create_pos_from_suggestions(db, {p: 30}, "u")[0]
    repo.mark_ordered(db, po_id)
    with pytest.raises(ValueError):
        repo.mark_ordered(db, po_id)  # already ordered
