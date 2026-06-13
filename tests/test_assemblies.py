import pytest

from digisearch.web.core import FeatureRegistry
from digisearch.web.core.db import Database
from digisearch.web.features.assemblies import feature as assemblies_feature
from digisearch.web.features.assemblies import importer, repo
from digisearch.web.features.catalog import feature as catalog_feature


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "a.db")
    reg = FeatureRegistry()
    reg.register(catalog_feature, assemblies_feature)
    database.apply_migrations(reg)
    return database


def _part(db, part_no, kind, minimrp_id):
    with db.connect() as conn:
        return conn.execute(
            "INSERT INTO parts (part_no, kind, minimrp_id) VALUES (?, ?, ?)",
            (part_no, kind, minimrp_id),
        ).lastrowid


def _setup(db):
    a = _part(db, "ASM-1", "ASSY", 100)
    s = _part(db, "SUB-1", "ASSY", 200)
    c1 = _part(db, "C1", "PART", 1)
    c2 = _part(db, "C2", "PART", 2)
    pm = {100: a, 200: s, 1: c1, 2: c2}
    importer.import_bom_rows(db, parts_map=pm, usedin=[
        {"AutoID": "1", "ParentID": "100", "ChildID": "1", "QtyPer": "2", "RefText": "R1, R2", "LineItemNo": "1"},
        {"AutoID": "2", "ParentID": "100", "ChildID": "200", "QtyPer": "1", "RefText": "", "LineItemNo": "2"},
        {"AutoID": "3", "ParentID": "200", "ChildID": "2", "QtyPer": "3", "RefText": "C5", "LineItemNo": "1"},
    ])
    return a, s, c1, c2


def test_import_and_list(db):
    _setup(db)
    by = {a["part_no"]: a for a in repo.list_assemblies(db)}
    assert set(by) == {"ASM-1", "SUB-1"}
    assert by["ASM-1"]["line_count"] == 2 and by["ASM-1"]["used_in"] == 0
    assert by["SUB-1"]["line_count"] == 1 and by["SUB-1"]["used_in"] == 1  # used in ASM-1


def test_get_assembly_children_subassembly_and_where_used(db):
    a, s, c1, c2 = _setup(db)
    top = repo.get_assembly(db, a)
    kinds = {ln["child_part_no"]: ln["child_kind"] for ln in top["lines"]}
    assert kinds == {"C1": "PART", "SUB-1": "ASSY"}  # a subassembly child
    c1line = next(ln for ln in top["lines"] if ln["child_part_no"] == "C1")
    assert c1line["qty_per"] == 2 and c1line["refdes"] == "R1, R2"
    assert top["used_in"] == []

    sub = repo.get_assembly(db, s)
    assert [ln["child_part_no"] for ln in sub["lines"]] == ["C2"]
    assert [u["part_no"] for u in sub["used_in"]] == ["ASM-1"]  # where-used


def test_summary_and_idempotent_import(db):
    a, s, c1, c2 = _setup(db)
    assert repo.summary(db) == {"assemblies": 2, "lines": 3, "empty": 0}
    importer.import_bom_rows(db, parts_map={100: a, 1: c1}, usedin=[
        {"AutoID": "1", "ParentID": "100", "ChildID": "1", "QtyPer": "2", "LineItemNo": "1"},
    ])
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM bom_lines").fetchone()[0] == 3  # no duplicate


def test_unmapped_rows_are_skipped(db):
    a = _part(db, "ASM-1", "ASSY", 100)
    stats = importer.import_bom_rows(db, parts_map={100: a}, usedin=[
        {"AutoID": "1", "ParentID": "100", "ChildID": "999", "QtyPer": "1"},  # child not in map
    ])
    assert stats == {"bom_lines": 0, "skipped": 1}


def test_get_assembly_rejects_non_assembly(db):
    a, s, c1, c2 = _setup(db)
    assert repo.get_assembly(db, c1) is None  # C1 is a PART, not an assembly


def test_create_assembly(db):
    new_id = repo.create_assembly(
        db, {"part_no": "98-NEW-1", "value": "New product", "rev": "A", "category": "PRODUCT"}
    )
    a = repo.get_assembly(db, new_id)
    assert a is not None and a["part_no"] == "98-NEW-1" and a["kind"] == "ASSY"
    assert a["lines"] == []  # no BOM yet
    assert any(x["part_no"] == "98-NEW-1" for x in repo.list_assemblies(db))


def test_update_assembly_fields(db):
    aid = repo.create_assembly(db, {"part_no": "A1", "value": "v", "rev": "A"})
    repo.update_assembly(db, aid, {"part_no": "A1-REV", "value": "new", "rev": "B",
                                   "category": "PRODUCT", "description": "desc"})
    a = repo.get_assembly(db, aid)
    assert a["part_no"] == "A1-REV" and a["rev"] == "B" and a["value"] == "new"
    assert a["category"] == "PRODUCT" and a["description"] == "desc"
    assert a["kind"] == "ASSY"  # still an assembly


def test_add_and_delete_bom_line(db):
    a, s, c1, c2 = _setup(db)  # ASM-1 already has lines 1 (C1) and 2 (SUB-1)
    repo.add_bom_line(db, a, c2, 4, "U1")
    new = next(ln for ln in repo.get_assembly(db, a)["lines"] if ln["child_part_no"] == "C2")
    assert new["qty_per"] == 4 and new["refdes"] == "U1"
    assert new["line_no"] == 3  # auto-assigned max+1

    repo.delete_bom_line(db, a, new["id"])
    assert all(ln["child_part_no"] != "C2" for ln in repo.get_assembly(db, a)["lines"])


def test_add_bom_line_rejects_self_reference(db):
    a, s, c1, c2 = _setup(db)
    with pytest.raises(ValueError):
        repo.add_bom_line(db, a, a, 1, None)


def test_parts_for_picker_excludes_self(db):
    a, s, c1, c2 = _setup(db)
    ids = {p["id"] for p in repo.parts_for_picker(db, a)}
    assert a not in ids and {s, c1, c2} <= ids


def _component(db, part_no, unit_price):
    from digisearch.web.features.catalog import repo as catrepo
    lines = ([{"supplier_name": "DK", "unit_price": unit_price, "reel_qty": 1, "is_default": True}]
             if unit_price is not None else [])
    return catrepo.create_part(db, part={"part_no": part_no}, supplier_lines=lines)


def test_assembly_line_and_total_costs(db):
    asm = repo.create_assembly(db, {"part_no": "COST-ASM"})
    repo.add_bom_line(db, asm, _component(db, "P1", 0.5), 3, "R1")    # 3 x 0.5 = 1.5
    repo.add_bom_line(db, asm, _component(db, "P2", 2.0), 2, "R2")    # 2 x 2.0 = 4.0
    repo.add_bom_line(db, asm, _component(db, "P3", None), 5, "R3")   # no cost -> blank
    a = repo.get_assembly(db, asm)
    costs = {ln["child_part_no"]: (ln["unit_cost"], ln["line_cost"]) for ln in a["lines"]}
    assert costs["P1"] == (0.5, 1.5)
    assert costs["P2"] == (2.0, 4.0)
    assert costs["P3"][1] is None        # unknown component cost -> no line cost
    assert a["total_cost"] == 5.5        # 1.5 + 4.0


def test_subassembly_cost_rolls_up(db):
    sub = repo.create_assembly(db, {"part_no": "SUB"})
    repo.add_bom_line(db, sub, _component(db, "LEAF", 1.0), 4, "")    # sub = 4 x 1.0 = 4.0
    top = repo.create_assembly(db, {"part_no": "TOP"})
    repo.add_bom_line(db, top, sub, 2, "")                           # 2 subs x 4.0 = 8.0
    a = repo.get_assembly(db, top)
    line = a["lines"][0]
    assert line["unit_cost"] == 4.0 and line["line_cost"] == 8.0
    assert a["total_cost"] == 8.0


# ---- CSV BOM import (reuses purchasing resolution) ----

def _rline(refdes, qty, value, *, chosen=None, status=None, stock_match=None):
    from digisearch.models import BomLine, LineKind, ResolvedLine, Status
    return ResolvedLine(
        line=BomLine(refdes=[refdes], qty=qty, value=value), kind=LineKind.MPN,
        chosen=chosen, status=status or Status.RESOLVED, stock_match=stock_match,
    )


def _cand(mpn, supplier="Digi-Key"):
    from digisearch.models import Candidate
    return Candidate(supplier=supplier, mpn=mpn, manufacturer="ACME",
                     dk_part_number=mpn + "-ND", unit_price=0.1, reel_qty=5000,
                     product_url=f"https://www.digikey.com/{mpn}")


def test_build_import_plan_classifies(db):
    from digisearch.models import Status
    from digisearch.web.features.assemblies import import_bom
    from digisearch.web.features.catalog import repo as catrepo
    from digisearch.web.features.purchasing.service import ResolvedRun

    existing = catrepo.create_part(db, part={"part_no": "EXIST-MPN", "value": "10k"},
                                   supplier_lines=[])
    run = ResolvedRun(
        resolved=[
            _rline("R1", 1, "10k", chosen=_cand("EXIST-MPN")),         # already in catalog
            _rline("R2", 2, "4k7", chosen=_cand("NEW-MPN-1")),         # resolved, new
            _rline("R3", 1, "weird", status=Status.NOT_FOUND),         # unresolved
            _rline("R4", 1, "DNM", status=Status.DNP),                 # skip
            _rline("L1", 1, "", status=Status.MANUAL),                 # under-specified
        ],
        build_qty=1, currency="SEK", stock_checked=False, mouser_enabled=False,
    )
    plan = import_bom.build_import_plan(db, run)
    assert [it["status"] for it in plan] == [
        "in_inventory", "new", "unresolved", "skip", "manual"]
    assert plan[0]["part_id"] == existing
    assert plan[1]["part_no"] == "NEW-MPN-1" and plan[1]["supplier_name"] == "Digi-Key"
    assert plan[1]["product_url"]  # the verify link is carried into the review screen


def test_apply_import_plan_creates_and_links(db):
    from digisearch.web.features.assemblies import import_bom
    from digisearch.web.features.catalog import repo as catrepo

    asm = repo.create_assembly(db, {"part_no": "ASM-IMP"})
    existing = catrepo.create_part(db, part={"part_no": "EXIST-MPN"}, supplier_lines=[])
    plan = [
        {"status": "in_inventory", "part_id": existing, "qty": 3, "refdes": "R1"},
        {"status": "new", "part_no": "NEW-1", "value": "4k7", "category": "RESISTOR",
         "mfr_pno": "NEW-1", "supplier_name": "Digi-Key", "supplier_pno": "NEW-1-ND",
         "unit_cost": 0.1, "reel_qty": 5000, "qty": 2, "refdes": "R2"},
        {"status": "unresolved", "part_no": "WEIRD", "value": "weird", "qty": 1, "refdes": "R3"},
        {"status": "new", "part_no": "SKIP-NEW", "qty": 1, "refdes": "R4"},   # not accepted
        {"status": "skip", "qty": 1, "refdes": "R5"},
    ]
    stats = import_bom.apply_import_plan(db, asm, plan, accepted={1, 2})
    assert stats["created"] == 2 and stats["lines_added"] == 3
    childs = {ln["child_part_no"] for ln in repo.get_assembly(db, asm)["lines"]}
    assert childs == {"EXIST-MPN", "NEW-1", "WEIRD"}  # SKIP-NEW left out
    assert catrepo.find_part_id_by_mpn(db, "NEW-1") is not None  # created in catalog


def test_convert_to_component_reclassifies_empty_assembly(db):
    from digisearch.web.features.catalog import repo as catrepo

    # A part mis-entered as an assembly, with stock but no BOM.
    aid = repo.create_assembly(db, {"part_no": "WAS-ASSY", "value": "really a part",
                                    "default_build_days": 5})
    with db.connect() as conn:
        conn.execute("UPDATE parts SET total_qty = 7 WHERE id = ?", (aid,))
        conn.commit()

    repo.convert_to_component(db, aid)

    part = catrepo.get_part(db, aid)
    assert part["kind"] == "PART" and part["default_build_days"] is None
    assert part["total_qty"] == 7                       # stock preserved
    assert repo.get_assembly(db, aid) is None           # no longer an assembly
    # shows up in the parts catalog now
    assert any(p["part_no"] == "WAS-ASSY" for p in catrepo.list_parts(db)[0])


def test_convert_blocked_when_bom_present(db):
    a, s, c1, c2 = _setup(db)
    with pytest.raises(ValueError, match="BOM line"):
        repo.convert_to_component(db, a)
    assert repo.get_assembly(db, a) is not None          # untouched


def test_convert_rejects_non_assembly(db):
    from digisearch.web.features.catalog import repo as catrepo
    pid = catrepo.create_part(db, part={"part_no": "PLAIN"}, supplier_lines=[])
    with pytest.raises(ValueError, match="not an assembly"):
        repo.convert_to_component(db, pid)


def test_convert_used_in_links_survive(db):
    # SUB-1 (an ASSY) is used in ASM-1; emptying SUB-1's own BOM lets it become a component
    # while staying a child of ASM-1.
    a, s, c1, c2 = _setup(db)
    for ln in repo.get_assembly(db, s)["lines"]:
        repo.delete_bom_line(db, s, ln["id"])
    repo.convert_to_component(db, s)
    parent = repo.get_assembly(db, a)
    assert "SUB-1" in {ln["child_part_no"] for ln in parent["lines"]}
