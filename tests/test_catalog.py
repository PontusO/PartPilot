import pytest

from digisearch.web.core import FeatureRegistry
from digisearch.web.core.db import Database
from digisearch.web.features.catalog import feature as catalog_feature
from digisearch.web.features.catalog import importer, pricing, repo, stock


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "c.db")
    reg = FeatureRegistry()
    reg.register(catalog_feature)
    database.apply_migrations(reg)
    return database


SUPPLIERS = [{"AddID": "2", "CoName": "Digikey", "ShortNm": "DK", "URL": "", "defCurrency": "SEK"}]
PARTS = [
    {"ItemID": "1", "MasterPNo": "GRM155R61A106ME11D", "ItemName": "10uF/10V/20%/0402",
     "ItemDescription": "", "Category": "capacitor", "Type": "PART", "MfrName": "", "MfrPNo": "",
     "Rev": "", "xCost": "0.10580", "MinQty": "5000", "TotalQty": "9500", "TotalAllocQty": "0",
     "TotalOnOrderQty": "0"},
    {"ItemID": "329", "MasterPNo": "98-00195-1", "ItemName": "Widget assembly",
     "ItemDescription": "", "Category": "PRODUCT", "Type": "ASSY", "xCost": "", "MinQty": "0",
     "TotalQty": "5", "TotalAllocQty": "1", "TotalOnOrderQty": "0"},
]
ITEM_SUPPLIERS = [
    {"AutoID": "10", "Supplier_ItemID": "1", "SupplierID": "2",
     "SupplierPNo": "490-GRM155R61A106ME11DTR-ND", "PriceEach": "1058", "QtyPerUOM": "10000",
     "MinOrQty": "1", "LeadTime": "3", "DefaultSupplier": "1"},
]
ITEM_LOCATIONS = [
    {"AutoID": "100", "LocStockID": "1", "LocLocationID": "1", "LocBIN": "KH1",
     "LocOnHandQty": "9500", "LocAllocQty": "0", "LocOnOrderQty": "0"},
]


def _import(db):
    return importer.import_tables(
        db, suppliers=SUPPLIERS, parts=PARTS,
        item_suppliers=ITEM_SUPPLIERS, item_locations=ITEM_LOCATIONS,
    )


def test_import_counts(db):
    stats = _import(db)
    assert stats == {"suppliers": 1, "parts": 2, "locations": 1,
                     "part_suppliers": 1, "part_stock": 1}


def test_import_is_idempotent(db):
    _import(db)
    _import(db)  # re-run upserts, must not duplicate
    _, total = repo.list_parts(db)
    assert total == 1  # the ASSY (98-00195-1) is excluded from the Parts list
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM part_suppliers").fetchone()[0] == 1


def test_list_search_and_filter(db):
    _import(db)
    _, total = repo.list_parts(db)
    assert total == 1  # components only; the ASSY is excluded

    hits, n = repo.list_parts(db, search="GRM155")
    assert n == 1 and hits[0]["part_no"] == "GRM155R61A106ME11D"

    caps, n = repo.list_parts(db, category="CAPACITOR")  # category upper-cased on import
    assert n == 1 and caps[0]["category"] == "CAPACITOR"

    # Per-piece price = PriceEach / QtyPerUOM = 1058 / 10000.
    assert caps[0]["supplier"] == "Digikey"
    assert abs(caps[0]["unit_price"] - 0.1058) < 1e-9
    assert caps[0]["below_min"] is False  # 9500 free >= 5000 min


def test_get_part_detail(db):
    _import(db)
    hits, _ = repo.list_parts(db, search="GRM155")
    part = repo.get_part(db, hits[0]["id"])
    assert part["mfr_pno"] in (None, "")
    assert part["free"] == 9500
    assert len(part["suppliers"]) == 1
    s = part["suppliers"][0]
    assert s["is_default"] == 1 and abs(s["unit_price"] - 0.1058) < 1e-9
    assert len(part["stock"]) == 1 and part["stock"][0]["bin"] == "KH1"


def test_exclude_from_bom_cost_persists_and_documents_forced(db):
    # A normal component: honours the checkbox, defaults off.
    comp = repo.create_part(db, part={"part_no": "99-00001-1"}, supplier_lines=[])
    assert repo.get_part(db, comp)["exclude_from_bom_cost"] is False
    comp2 = repo.create_part(db, part={"part_no": "99-00002-1", "exclude_from_bom_cost": 1},
                             supplier_lines=[])
    assert repo.get_part(db, comp2)["exclude_from_bom_cost"] is True

    # Documents (5x class) are always excluded, regardless of the checkbox — on create and update.
    assert repo.is_document_part_no("54-00001-1") and not repo.is_document_part_no("99-00001-1")
    doc = repo.create_part(db, part={"part_no": "54-00001-1", "value": "Schematic"}, supplier_lines=[])
    assert repo.get_part(db, doc)["exclude_from_bom_cost"] is True
    repo.update_part(db, doc, part={"part_no": "54-00001-1", "exclude_from_bom_cost": 0},
                     supplier_lines=[])
    assert repo.get_part(db, doc)["exclude_from_bom_cost"] is True  # re-enforced on save


def test_is_document_flag_persists_and_forces_bom_exclusion(db):
    # Defaults off for an ordinary component.
    comp = repo.create_part(db, part={"part_no": "99-00001-1"}, supplier_lines=[])
    assert repo.get_part(db, comp)["is_document"] is False

    # Ticking the document box marks it a document AND forces it out of BOM cost, even for a
    # non-5x number and with exclude explicitly left off.
    manual = repo.create_part(
        db, part={"part_no": "99-00003-1", "is_document": 1, "exclude_from_bom_cost": 0},
        supplier_lines=[])
    got = repo.get_part(db, manual)
    assert got["is_document"] is True and got["exclude_from_bom_cost"] is True

    # 5x-class numbers are documents regardless of the box — on create and update.
    doc = repo.create_part(db, part={"part_no": "54-00009-1"}, supplier_lines=[])
    assert repo.get_part(db, doc)["is_document"] is True
    repo.update_part(db, doc, part={"part_no": "54-00009-1", "is_document": 0}, supplier_lines=[])
    assert repo.get_part(db, doc)["is_document"] is True  # re-enforced on save

    # Clearing the box on a non-5x part turns it back into a normal, includable component.
    repo.update_part(db, manual, part={"part_no": "99-00003-1", "is_document": 0}, supplier_lines=[])
    got = repo.get_part(db, manual)
    assert got["is_document"] is False and got["exclude_from_bom_cost"] is False


def test_normally_stocked_persists_and_defaults_off(db):
    pid = repo.create_part(db, part={"part_no": "NS-1"}, supplier_lines=[])
    assert repo.get_part(db, pid)["normally_stocked"] is False  # default unchecked

    pid2 = repo.create_part(db, part={"part_no": "NS-2", "normally_stocked": 1}, supplier_lines=[])
    assert repo.get_part(db, pid2)["normally_stocked"] is True

    repo.update_part(db, pid2, part={"part_no": "NS-2", "normally_stocked": 0}, supplier_lines=[])
    assert repo.get_part(db, pid2)["normally_stocked"] is False  # toggled back off


def test_list_parts_stocked_only_filter(db):
    repo.create_part(db, part={"part_no": "STK-1", "normally_stocked": 1}, supplier_lines=[])
    repo.create_part(db, part={"part_no": "CUST-1", "normally_stocked": 0}, supplier_lines=[])

    _, both = repo.list_parts(db)
    assert both == 2

    stocked, n = repo.list_parts(db, stocked_only=True)
    assert n == 1 and stocked[0]["part_no"] == "STK-1"
    assert stocked[0]["normally_stocked"] is True

    assert repo.summary(db)["normally_stocked"] == 1


def test_summary(db):
    _import(db)
    s = repo.summary(db)
    # ASSY/PRODUCT excluded; nothing seeded as normally_stocked (no BOM tree in this fixture)
    assert s == {"parts": 1, "categories": 1, "below_min": 0, "normally_stocked": 0}


def test_get_missing_part(db):
    _import(db)
    assert repo.get_part(db, 9999) is None


def test_parts_for_supplier(db):
    _import(db)  # GRM155... is supplied by "Digikey"
    parts = repo.parts_for_supplier(db, "digikey")  # case-insensitive
    assert any(p["part_no"] == "GRM155R61A106ME11D" for p in parts)
    assert parts[0]["unit_price"] is not None        # enriched with unit price
    assert repo.parts_for_supplier(db, "NoSuchSupplier") == []
    assert repo.parts_for_supplier(db, "") == []


def test_supplier_distributor_links(db):
    pid = repo.create_part(db, part={"part_no": "X"}, supplier_lines=[
        {"supplier_name": "Digi-Key", "supplier_pno": "490-ABC-ND", "unit_price": 0.1,
         "reel_qty": 1, "is_default": True},
        {"supplier_name": "Mouser Electronics", "supplier_pno": "81-XYZ", "unit_price": 0.2,
         "reel_qty": 1, "is_default": False},
        {"supplier_name": "Local Shop", "supplier_pno": "LS-1", "unit_price": 0.3,
         "reel_qty": 1, "is_default": False},
    ])
    sup = {s["supplier_name"]: s for s in repo.get_part(db, pid)["suppliers"]}
    # "Digi-Key" (hyphen) normalizes to match, and the part number is in the search URL
    assert "digikey.com" in sup["Digi-Key"]["part_url"] and "490-ABC-ND" in sup["Digi-Key"]["part_url"]
    assert "mouser.com" in sup["Mouser Electronics"]["part_url"]
    assert sup["Local Shop"]["part_url"] is None   # unknown distributor -> no link


def test_create_part_with_suppliers_and_stock(db):
    pid = repo.create_part(
        db,
        part={"part_no": "NEWPART1", "value": "10k/1%/0402", "category": "RESISTOR", "min_qty": 100},
        supplier_lines=[
            {"supplier_name": "Digikey", "supplier_pno": "DK-1", "unit_price": 0.1,
             "reel_qty": 5000, "is_default": True},
            {"supplier_name": "BrandNewSup", "supplier_pno": "BN-1", "unit_price": 0.08,
             "reel_qty": 1, "is_default": False},
        ],
        opening={"qty": 2500, "location_id": None, "bin": "A1"},
    )
    part = repo.get_part(db, pid)
    assert part["part_no"] == "NEWPART1" and part["kind"] == "PART"
    assert part["total_qty"] == 2500
    assert part["unit_cost"] == 0.1  # taken from the default supplier's unit price

    assert len(part["suppliers"]) == 2
    dk = next(s for s in part["suppliers"] if s["supplier_name"] == "Digikey")
    assert dk["price_per_uom"] == 500.0          # unit_price (0.1) * reel_qty (5000)
    assert abs(dk["unit_price"] - 0.1) < 1e-9     # computed back per piece
    assert dk["is_default"] == 1

    # a brand-new supplier was created on the fly
    assert any(s["name"] == "BrandNewSup" for s in repo.suppliers(db))
    # opening stock landed in a bin
    assert len(part["stock"]) == 1 and part["stock"][0]["bin"] == "A1"
    assert part["stock"][0]["on_hand"] == 2500


def test_update_part_changes_fields_suppliers_and_stock(db):
    pid = repo.create_part(
        db, part={"part_no": "EDIT1", "category": "RESISTOR", "min_qty": 10},
        supplier_lines=[{"supplier_name": "Digikey", "unit_price": 0.1, "reel_qty": 5000,
                         "is_default": True}],
        opening={"qty": 1000, "bin": "A1"},
    )
    repo.update_part(
        db, pid,
        part={"part_no": "EDIT1-REV", "category": "CAPACITOR", "value": "1u0", "min_qty": 50,
              "notes": "changed"},
        supplier_lines=[
            {"supplier_name": "Mouser Electronics", "supplier_pno": "MO-9", "unit_price": 0.2,
             "reel_qty": 4000, "is_default": True},
            {"supplier_name": "Farnell", "unit_price": 0.25, "reel_qty": 1, "is_default": False},
        ],
        stock={"qty": 750, "bin": "B2", "location_id": None},
    )
    p = repo.get_part(db, pid)
    assert p["part_no"] == "EDIT1-REV" and p["category"] == "CAPACITOR" and p["value"] == "1u0"
    assert p["min_qty"] == 50 and p["notes"] == "changed"
    assert p["total_qty"] == 750
    names = {s["supplier_name"] for s in p["suppliers"]}
    assert names == {"Mouser Electronics", "Farnell"}  # old Digikey line replaced
    mo = next(s for s in p["suppliers"] if s["supplier_name"] == "Mouser Electronics")
    assert mo["is_default"] == 1 and mo["price_per_uom"] == 800.0  # 0.2 * 4000
    assert p["unit_cost"] == 0.2  # from the new default supplier
    assert p["stock"][0]["bin"] == "B2" and p["stock"][0]["on_hand"] == 750


def test_stock_movements_receive_issue_adjust(db):
    pid = repo.create_part(
        db, part={"part_no": "MOVE1"},
        supplier_lines=[{"supplier_name": "X", "unit_price": 1.0, "reel_qty": 1, "is_default": True}],
        opening={"qty": 100, "bin": "A1"})
    assert repo.get_part(db, pid)["total_qty"] == 100

    stock.adjust_stock(db, pid, delta=50, mtype=stock.RECEIVE, reference="PO-1", user="u")
    assert repo.get_part(db, pid)["total_qty"] == 150
    stock.adjust_stock(db, pid, delta=-30, mtype=stock.ISSUE, reference="WO-1", user="u")
    assert repo.get_part(db, pid)["total_qty"] == 120

    mv = stock.movements_for_part(db, pid)          # newest first, with running balance
    assert [m["mtype"] for m in mv][:2] == ["ISSUE", "RECEIVE"]
    assert mv[0]["qty_delta"] == -30 and mv[0]["qty_after"] == 120
    assert mv[1]["qty_delta"] == 50 and mv[1]["qty_after"] == 150


def test_post_movement_creates_stock_row_when_none(db):
    pid = repo.create_part(
        db, part={"part_no": "NOSTOCK"},
        supplier_lines=[{"supplier_name": "X", "unit_price": 1.0, "reel_qty": 1, "is_default": True}])
    assert repo.get_part(db, pid)["total_qty"] == 0 and repo.get_part(db, pid)["stock"] == []
    stock.adjust_stock(db, pid, delta=10, mtype=stock.RECEIVE)
    p = repo.get_part(db, pid)
    assert p["total_qty"] == 10 and len(p["stock"]) == 1


@pytest.mark.parametrize("qty,expected", [
    (1, 0.10),      # below the first break -> smallest-break price
    (100, 0.10),    # exactly the first break
    (500, 0.10),    # between breaks -> highest break <= qty (still the 100 tier)
    (1000, 0.05),   # exactly the second break
    (5000, 0.05),   # above the largest break -> largest-break price
])
def test_price_at_selects_highest_break(qty, expected):
    tiers = [(100, 0.10), (1000, 0.05)]
    assert abs(pricing.price_at(tiers, qty) - expected) < 1e-9


def test_price_at_between_three_tiers():
    tiers = [(100, 0.10), (1000, 0.05), (10000, 0.02)]
    assert pricing.price_at(tiers, 500) == 0.10
    assert pricing.price_at(tiers, 1500) == 0.05
    assert pricing.price_at(tiers, 50000) == 0.02


def test_price_at_empty_is_none():
    assert pricing.price_at([], 100) is None


def _part_with_cost_tiers(db, part_no="TIERED"):
    """A part whose default supplier carries auto-captured cut-tape cost tiers."""
    return repo.create_part(
        db,
        part={"part_no": part_no, "category": "RESISTOR"},
        supplier_lines=[{
            "supplier_name": "Digikey", "supplier_pno": "DK-T1", "unit_price": 0.10,
            "reel_qty": 1, "is_default": True,
            "cost_tiers": [{"break_qty": 1, "unit_price": 0.10},
                           {"break_qty": 100, "unit_price": 0.06},
                           {"break_qty": 1000, "unit_price": 0.03}],
        }],
    )


def test_create_part_captures_cost_tiers(db):
    pid = _part_with_cost_tiers(db)
    part = repo.get_part(db, pid)
    tiers = part["suppliers"][0]["cost_tiers"]
    assert [(t["break_qty"], t["unit_price"]) for t in tiers] == [(1, 0.10), (100, 0.06), (1000, 0.03)]
    assert all(t["kind"] == "cut" for t in tiers)


def test_update_part_preserves_cost_tiers_on_edit(db):
    """An ordinary edit (change price, keep the same supplier+P/N) must NOT drop cost tiers."""
    pid = _part_with_cost_tiers(db)
    ps_id_before = repo.get_part(db, pid)["suppliers"][0]["id"]
    # Edit: same supplier + P/N, different unit price, no cost_tiers submitted (form is read-only).
    repo.update_part(
        db, pid,
        part={"part_no": "TIERED"},
        supplier_lines=[{"supplier_name": "Digikey", "supplier_pno": "DK-T1", "unit_price": 0.12,
                         "reel_qty": 1, "is_default": True}],
    )
    part = repo.get_part(db, pid)
    assert part["suppliers"][0]["id"] == ps_id_before        # row id survived (reconciled in place)
    assert len(part["suppliers"][0]["cost_tiers"]) == 3       # tiers preserved


def test_update_part_drops_removed_suppliers_tiers(db):
    """Removing a supplier offer removes its cost tiers (CASCADE)."""
    pid = _part_with_cost_tiers(db)
    repo.update_part(
        db, pid,
        part={"part_no": "TIERED"},
        supplier_lines=[{"supplier_name": "Farnell", "supplier_pno": "FN-1", "unit_price": 0.2,
                         "reel_qty": 1, "is_default": True}],
    )
    part = repo.get_part(db, pid)
    assert {s["supplier_name"] for s in part["suppliers"]} == {"Farnell"}
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM part_supplier_tiers").fetchone()[0] == 0


def test_sell_tier_crud(db):
    pid = repo.create_part(db, part={"part_no": "SELL-1"}, supplier_lines=[])
    repo.replace_sell_tiers(db, pid, [{"break_qty": 1, "unit_price": 1.0},
                                      {"break_qty": 100, "unit_price": 0.8}])
    sell = repo.get_part(db, pid)["sell_tiers"]
    assert [(t["break_qty"], t["unit_price"], t["source"]) for t in sell] == \
        [(1, 1.0, "manual"), (100, 0.8, "manual")]
    # replace wipes the old manual rows
    repo.replace_sell_tiers(db, pid, [{"break_qty": 50, "unit_price": 0.9}])
    sell = repo.get_part(db, pid)["sell_tiers"]
    assert [(t["break_qty"], t["source"]) for t in sell] == [(50, "manual")]


def test_generate_sell_tiers_from_cost_uses_markup(db):
    pid = _part_with_cost_tiers(db)             # cut tiers 0.10 / 0.06 / 0.03
    written = repo.generate_sell_tiers_from_cost(db, pid, default_markup=2.0)
    assert written == 3
    sell = {t["break_qty"]: t["unit_price"] for t in repo.get_part(db, pid)["sell_tiers"]}
    assert sell == pytest.approx({1: 0.20, 100: 0.12, 1000: 0.06})

    # A per-part markup override beats the default.
    repo.update_part(db, pid, part={"part_no": "TIERED", "markup": 3.0},
                     supplier_lines=[{"supplier_name": "Digikey", "supplier_pno": "DK-T1",
                                      "unit_price": 0.10, "reel_qty": 1, "is_default": True}])
    repo.generate_sell_tiers_from_cost(db, pid, default_markup=2.0)
    sell = {t["break_qty"]: t["unit_price"] for t in repo.get_part(db, pid)["sell_tiers"]}
    assert sell == pytest.approx({1: 0.30, 100: 0.18, 1000: 0.09})


def test_generate_sell_tiers_preserves_manual(db):
    pid = _part_with_cost_tiers(db)
    repo.replace_sell_tiers(db, pid, [{"break_qty": 100, "unit_price": 0.50}])  # manual at 100
    repo.generate_sell_tiers_from_cost(db, pid, default_markup=2.0)
    sell = {t["break_qty"]: (t["unit_price"], t["source"]) for t in repo.get_part(db, pid)["sell_tiers"]}
    assert sell[100] == (0.50, "manual")     # manual wins at the shared break qty
    assert sell[1][1] == "markup" and sell[1000][1] == "markup"


def test_recalc_sell_tiers_rebased_from_purchase(db):
    pid = repo.create_part(db, part={"part_no": "RB-1"}, supplier_lines=[{
        "supplier_name": "Digi-Key", "supplier_pno": "RB-1-ND", "unit_price": 2.0, "reel_qty": 1,
        "is_default": True,
        "cost_tiers": [{"break_qty": 1, "unit_price": 2.00}, {"break_qty": 100, "unit_price": 1.50},
                       {"break_qty": 1000, "unit_price": 1.00}, {"break_qty": 5000, "unit_price": 0.80}]}])
    # Negotiated 0.90 at the 1000 tier -> the whole ladder rebases by 0.90/1.00, x markup 1.30.
    n = repo.recalc_sell_tiers_from_purchase(db, pid, anchor_qty=1000, anchor_price=0.90,
                                             default_markup=1.30)
    assert n == 4
    sell = {t["break_qty"]: t["unit_price"] for t in repo.get_part(db, pid)["sell_tiers"]}
    assert sell == pytest.approx({1: 2.34, 100: 1.755, 1000: 1.17, 5000: 0.936})

    # Anchored at list (1.00) collapses to plain cost x markup.
    repo.recalc_sell_tiers_from_purchase(db, pid, 1000, 1.00, 1.30)
    sell = {t["break_qty"]: t["unit_price"] for t in repo.get_part(db, pid)["sell_tiers"]}
    assert sell == pytest.approx({1: 2.60, 100: 1.95, 1000: 1.30, 5000: 1.04})


def test_recalc_sell_tiers_preserves_manual(db):
    pid = repo.create_part(db, part={"part_no": "RB-2"}, supplier_lines=[{
        "supplier_name": "Digi-Key", "supplier_pno": "RB-2-ND", "unit_price": 2.0, "reel_qty": 1,
        "is_default": True,
        "cost_tiers": [{"break_qty": 1, "unit_price": 2.00}, {"break_qty": 100, "unit_price": 1.50},
                       {"break_qty": 1000, "unit_price": 1.00}]}])
    repo.replace_sell_tiers(db, pid, [{"break_qty": 100, "unit_price": 1.90}])   # manual at 100
    repo.recalc_sell_tiers_from_purchase(db, pid, 1000, 1.00, 1.30)
    sell = {t["break_qty"]: (t["unit_price"], t["source"]) for t in repo.get_part(db, pid)["sell_tiers"]}
    assert sell[100] == (1.90, "manual")           # manual preserved, wins at its break
    assert sell[1][1] == "markup" and sell[1000][1] == "markup"


def test_recalc_sell_tiers_no_cost_tiers_is_noop(db):
    pid = repo.create_part(db, part={"part_no": "RB-3"}, supplier_lines=[{
        "supplier_name": "Local", "supplier_pno": "L", "unit_price": 2.0, "reel_qty": 1,
        "is_default": True}])
    assert repo.recalc_sell_tiers_from_purchase(db, pid, 1000, 0.9, 1.30) == 0
    assert repo.get_part(db, pid)["sell_tiers"] == []


def test_leaf_and_rolled_sell_price(db):
    """rolled_sell_price is volume-aware over a 2-level BOM (needs the assemblies feature)."""
    from digisearch.web.features.assemblies import feature as asm_feature

    database = db
    # Rebuild with assemblies registered so bom_lines exists.
    reg = FeatureRegistry()
    reg.register(catalog_feature)
    reg.register(asm_feature)
    database.apply_migrations(reg)

    leaf = _part_with_cost_tiers(database, part_no="LEAF")   # cut 0.10/0.06/0.03, no sell tiers
    # leaf priced by cost x markup: at qty 1 -> 0.10*2, at qty 1000 -> 0.03*2
    with database.connect() as conn:
        assert pricing.leaf_sell_unit(conn, leaf, 1, 2.0) == pytest.approx(0.20)
        assert pricing.leaf_sell_unit(conn, leaf, 1000, 2.0) == pytest.approx(0.06)

    # Build an assembly using 5 of the leaf per unit.
    asy = repo.create_part(database, part={"part_no": "ASY-1"}, supplier_lines=[])
    with database.connect() as conn:
        conn.execute("UPDATE parts SET kind = 'ASSY' WHERE id = ?", (asy,))
        conn.execute("INSERT INTO bom_lines (parent_id, child_id, qty_per) VALUES (?, ?, ?)",
                     (asy, leaf, 5))
        conn.commit()
        # Build 200 units -> 1000 leaves total -> leaf at the 1000 tier (0.03*2=0.06); x5 per unit.
        assert pricing.rolled_sell_price(conn, asy, 200, 2.0) == pytest.approx(0.06 * 5)
        # Build 1 unit -> 5 leaves -> leaf at the 1 tier (0.10*2=0.20); x5.
        assert pricing.rolled_sell_price(conn, asy, 1, 2.0) == pytest.approx(0.20 * 5)


def test_refresh_cost_tiers_from_distributor(db, monkeypatch):
    from digisearch.models import Candidate
    from digisearch.web.features.catalog import cost_refresh

    pid = repo.create_part(db, part={"part_no": "RF-1"}, supplier_lines=[
        {"supplier_name": "Digi-Key", "supplier_pno": "DK-XYZ-ND", "unit_price": 0.1,
         "reel_qty": 1, "is_default": True,
         "cost_tiers": [{"break_qty": 1, "unit_price": 0.10}]},   # a stale tier to be replaced
        {"supplier_name": "Local Shop", "supplier_pno": "LS-1", "unit_price": 0.2,
         "reel_qty": 1, "is_default": False},
    ])
    cand = Candidate(supplier="Digi-Key", mpn="XYZ", dk_part_number="DK-XYZ-ND",
                     price_breaks=[(1, 0.09), (100, 0.05), (1000, 0.02)],
                     reel_price_breaks=[(5000, 0.01)])

    class FakeDK:
        def keyword_search(self, kw, limit=5):
            assert kw == "DK-XYZ-ND"       # looked up by the offer's supplier P/N
            return [cand]

    monkeypatch.setattr(cost_refresh, "_build_clients", lambda: (FakeDK(), None))
    result = cost_refresh.refresh_cost_tiers(db, pid)

    assert len(result["updated"]) == 1 and "Digi-Key" in result["updated"][0]
    assert any("Local Shop" in s for s in result["skipped"])   # not a distributor -> skipped

    part = repo.get_part(db, pid)
    dk = next(s for s in part["suppliers"] if s["supplier_name"] == "Digi-Key")
    cut = sorted((t["break_qty"], t["unit_price"]) for t in dk["cost_tiers"] if t["kind"] == "cut")
    reel = [(t["break_qty"], t["unit_price"]) for t in dk["cost_tiers"] if t["kind"] == "reel"]
    assert cut == [(1, 0.09), (100, 0.05), (1000, 0.02)]   # stale (1, 0.10) fully replaced
    assert reel == [(5000, 0.01)]
    # the non-distributor offer was left untouched (no tiers)
    ls = next(s for s in part["suppliers"] if s["supplier_name"] == "Local Shop")
    assert ls["cost_tiers"] == []


def test_refresh_cost_tiers_reports_unconfigured(db, monkeypatch):
    from digisearch.web.features.catalog import cost_refresh

    pid = repo.create_part(db, part={"part_no": "RF-2"}, supplier_lines=[
        {"supplier_name": "Mouser", "supplier_pno": "81-ABC", "unit_price": 0.1,
         "reel_qty": 1, "is_default": True}])
    monkeypatch.setattr(cost_refresh, "_build_clients", lambda: (None, None))  # neither configured
    result = cost_refresh.refresh_cost_tiers(db, pid)
    assert result["updated"] == []
    assert any("not configured" in e for e in result["errors"])


def test_create_part_reuses_existing_supplier_by_name(db):
    _import(db)  # has "Digikey"
    before = len(repo.suppliers(db))
    pid = repo.create_part(
        db, part={"part_no": "REUSE1"},
        supplier_lines=[{"supplier_name": "digikey", "unit_price": 1.0, "reel_qty": 1,
                         "is_default": True}],
    )
    assert len(repo.suppliers(db)) == before  # matched case-insensitively, no duplicate
    assert repo.get_part(db, pid)["suppliers"][0]["supplier_name"] == "Digikey"


def test_document_class_includes_95_software(db):
    # 95 (software / source code) is a document class like 5x — flags forced on create.
    assert repo.is_document_part_no("95-00001-1")
    assert not repo.is_document_part_no("96-00001-1")
    pid = repo.create_part(db, part={"part_no": "95-00001-1"}, supplier_lines=[])
    p = repo.get_part(db, pid)
    assert p["is_document"] is True and p["exclude_from_bom_cost"] is True


def test_importer_flags_document_class_parts(db):
    rows = PARTS + [{"ItemID": "500", "MasterPNo": "54-00007-1", "ItemName": "Drawing",
                     "ItemDescription": "", "Category": "", "Type": "PART", "xCost": "",
                     "MinQty": "0", "TotalQty": "0", "TotalAllocQty": "0", "TotalOnOrderQty": "0"}]
    importer.import_tables(db, suppliers=SUPPLIERS, parts=rows,
                           item_suppliers=ITEM_SUPPLIERS, item_locations=ITEM_LOCATIONS)
    doc = repo.find_part_by_part_no(db, "54-00007-1")
    p = repo.get_part(db, doc["id"])
    assert p["is_document"] is True and p["exclude_from_bom_cost"] is True
    normal = repo.find_part_by_part_no(db, "GRM155R61A106ME11D")
    assert repo.get_part(db, normal["id"])["is_document"] is False


def test_opening_stock_and_part_edit_go_through_the_ledger(db):
    from digisearch.web.features.catalog import stock as cstock

    pid = repo.create_part(db, part={"part_no": "L-1"}, supplier_lines=[],
                           opening={"qty": 30, "bin": "A1"})
    moves = cstock.movements_for_part(db, pid)
    assert [m["mtype"] for m in moves] == ["OPENING"] and moves[0]["qty_after"] == 30
    part = repo.get_part(db, pid)
    assert part["total_qty"] == 30 and part["stock"][0]["bin"] == "A1"

    # editing on-hand on the part form posts an ADJUST with the delta
    repo.update_part(db, pid, part={"part_no": "L-1"}, supplier_lines=[], stock={"qty": 25})
    moves = cstock.movements_for_part(db, pid)
    assert moves[0]["mtype"] == "ADJUST" and moves[0]["qty_delta"] == -5
    assert moves[0]["qty_after"] == 25 == repo.get_part(db, pid)["total_qty"]
    # an unchanged qty posts nothing
    repo.update_part(db, pid, part={"part_no": "L-1"}, supplier_lines=[], stock={"qty": 25})
    assert len(cstock.movements_for_part(db, pid)) == 2
