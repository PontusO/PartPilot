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
