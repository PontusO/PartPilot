import pytest

from partpilot.web.core.db import Database
from partpilot.web.core import FeatureRegistry
from partpilot.web.features.assemblies import feature as assemblies_feature
from partpilot.web.features.catalog import feature as catalog_feature
from partpilot.web.features.contacts import feature as contacts_feature
from partpilot.web.features.catalog import import_parts
from partpilot.web.features.catalog import repo as catrepo


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "a.db")
    reg = FeatureRegistry()
    reg.register(catalog_feature, contacts_feature, assemblies_feature)
    database.apply_migrations(reg)
    return database


def test_apply_parts_plan_creates_links_and_reports(db):
    existing = catrepo.create_part(db, part={"part_no": "EXIST-MPN"}, supplier_lines=[])
    plan = [
        {"status": "in_inventory", "part_id": existing, "qty": 3, "refdes": "R1"},
        {"status": "new", "part_no": "NEW-1", "value": "4k7", "category": "RESISTOR",
         "mfr_pno": "NEW-1", "supplier_name": "Digi-Key", "supplier_pno": "NEW-1-ND",
         "unit_cost": 0.1, "reel_qty": 5000, "qty": 2, "refdes": "R2"},
        {"status": "unresolved", "part_no": "WEIRD", "value": "weird", "qty": 1, "refdes": "R3"},
        {"status": "new", "part_no": "SKIP-NEW", "qty": 1, "refdes": "R4"},   # not accepted
        {"status": "skip", "qty": 1, "refdes": "R5"},
        {"status": "manual", "part_no": "???", "qty": 1, "refdes": "L1"},
    ]
    res = import_parts.apply_parts_plan(db, plan, accepted={1, 2})
    assert res["created"] == 2 and res["linked"] == 1 and res["skipped"] == 3
    assert res["part_ids"][0] == existing
    assert res["part_ids"][1] == catrepo.find_part_id_by_mpn(db, "NEW-1")
    assert res["part_ids"][2] == catrepo.find_part_id_by_mpn(db, "WEIRD")
    assert res["part_ids"][3] is None and res["part_ids"][4] is None and res["part_ids"][5] is None


def test_apply_parts_plan_dedupes_same_mpn(db):
    plan = [
        {"status": "new", "part_no": "DUP-1", "mfr_pno": "DUP-1", "qty": 1, "refdes": "C1"},
        {"status": "new", "part_no": "DUP-1", "mfr_pno": "DUP-1", "qty": 1, "refdes": "C2"},
    ]
    res = import_parts.apply_parts_plan(db, plan, accepted={0, 1})
    assert res["created"] == 1 and res["linked"] == 1
    assert res["part_ids"][0] == res["part_ids"][1]


def test_apply_parts_plan_reports_incomplete_values(db):
    plan = [{"status": "new", "part_no": "P-1", "value": "10k//0402",
             "value_missing": ["tolerance"], "qty": 1, "refdes": "R1"}]
    res = import_parts.apply_parts_plan(db, plan, accepted={0})
    assert res["review"] == [{"part_id": res["part_ids"][0], "part_no": "P-1",
                              "value": "10k//0402", "missing": ["tolerance"]}]


def test_assembly_wrapper_still_exposes_engine_names():
    from partpilot.web.features.assemblies import import_bom
    assert import_bom.build_import_plan is import_parts.build_import_plan
    assert import_bom.NEW == "new"


def test_apply_parts_plan_reports_which_rows_were_created(db):
    existing = catrepo.create_part(db, part={"part_no": "EXIST-MPN"}, supplier_lines=[])
    plan = [
        {"status": "new", "part_no": "DUP-1", "qty": 1, "refdes": "C1"},
        {"status": "new", "part_no": "DUP-1", "qty": 1, "refdes": "C2"},        # links to row 0's part
        {"status": "new", "part_no": "EXIST-MPN", "qty": 1, "refdes": "C3"},    # links to pre-existing
        {"status": "in_inventory", "part_id": existing, "qty": 1, "refdes": "C4"},
    ]
    res = import_parts.apply_parts_plan(db, plan, accepted={0, 1, 2})
    assert res["created_rows"] == [0]
    assert res["created"] == 1 and res["linked"] == 3


def test_apply_parts_plan_neither_links_nor_creates_a_retired_match(db):
    from partpilot.web.features.catalog import lifecycle
    old = catrepo.create_part(db, part={"part_no": "RET-1"}, supplier_lines=[])
    lifecycle.retire_part(db, old)
    plan = [{"status": "new", "part_no": "RET-1", "mfr_pno": "RET-1", "qty": 1, "refdes": "C1"}]
    res = import_parts.apply_parts_plan(db, plan, accepted={0})
    assert (res["created"], res["linked"], res["skipped"]) == (0, 0, 1)
    assert res["part_ids"] == [None]
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM parts WHERE part_no = 'RET-1'").fetchone()[0] == 1


def test_build_import_plan_flags_retired_match(db):
    from partpilot.models import BomLine, Candidate, LineKind, ResolvedLine, Status
    from partpilot.web.features.catalog import lifecycle
    from partpilot.web.features.purchasing.service import ResolvedRun
    old = catrepo.create_part(db, part={"part_no": "RET-1"}, supplier_lines=[])
    lifecycle.retire_part(db, old)
    line = ResolvedLine(
        line=BomLine(refdes=["C1"], qty=1, value="RET-1"), kind=LineKind.MPN,
        chosen=Candidate(supplier="Digi-Key", mpn="RET-1", dk_part_number="RET-1-ND"),
        status=Status.RESOLVED, confidence=0.9)
    run = ResolvedRun(resolved=[line], build_qty=1, currency="SEK", stock_checked=False,
                      mouser_enabled=False, warnings=[], stock_shelf=None)
    item = import_parts.build_import_plan(db, run)[0]
    assert item["status"] == "new" and item["part_id"] is None
    assert item["note"] == "matches retired part RET-1"
