"""Part lifecycle: retire (whole version group) → admin delete, blocked by any referencing row."""

import pytest

from partpilot.web.core import FeatureRegistry
from partpilot.web.core.db import Database
from partpilot.web.features.assemblies import feature as assemblies_feature
from partpilot.web.features.assemblies import repo as asmrepo
from partpilot.web.features.catalog import feature as catalog_feature
from partpilot.web.features.catalog import lifecycle, versions
from partpilot.web.features.catalog import repo as catrepo
from partpilot.web.features.contacts import feature as contacts_feature
from partpilot.web.features.contacts import repo as conrepo
from partpilot.web.features.customer_orders import feature as co_feature
from partpilot.web.features.customer_orders import repo as corepo
from partpilot.web.features.purchase_orders import feature as po_feature
from partpilot.web.features.purchase_orders import repo as porepo
from partpilot.web.features.work_orders import feature as wo_feature
from partpilot.web.features.work_orders import repo as worepo


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "lc.db")
    reg = FeatureRegistry()
    reg.register(catalog_feature, assemblies_feature, contacts_feature, co_feature,
                 wo_feature, po_feature)
    database.apply_migrations(reg)
    return database


@pytest.fixture
def catalog_only(tmp_path):
    database = Database(tmp_path / "c.db")
    reg = FeatureRegistry()
    reg.register(catalog_feature, contacts_feature)
    database.apply_migrations(reg)
    return database


def _comp(db, part_no, qty=0):
    return catrepo.create_part(
        db, part={"part_no": part_no},
        supplier_lines=[{"supplier_name": "X", "unit_price": 1, "reel_qty": 1, "is_default": True}],
        opening={"qty": qty} if qty else None)


def _retired_at(db, pid):
    with db.connect() as conn:
        return conn.execute("SELECT retired_at FROM parts WHERE id = ?", (pid,)).fetchone()[0]


def test_retire_stamps_every_version_in_group(db):
    pid = _comp(db, "R-1")
    vid = versions.create_version(db, pid, rev="B")
    lifecycle.retire_part(db, vid)
    assert _retired_at(db, pid) is not None
    assert _retired_at(db, pid) == _retired_at(db, vid)


def test_unretire_clears_every_version_in_group(db):
    pid = _comp(db, "R-1")
    vid = versions.create_version(db, pid, rev="B")
    lifecycle.retire_part(db, pid)
    lifecycle.unretire_part(db, vid)
    assert _retired_at(db, pid) is None and _retired_at(db, vid) is None


def test_retire_unknown_part_raises(db):
    with pytest.raises(ValueError, match="Part not found"):
        lifecycle.retire_part(db, 999999)


def test_delete_refuses_unretired_part(db):
    pid = _comp(db, "R-1")
    with pytest.raises(ValueError, match=r"Retire R-1 before deleting it\."):
        lifecycle.delete_part(db, pid)
    assert catrepo.get_part(db, pid) is not None


def test_delete_removes_group_and_cascades(db):
    pid = _comp(db, "R-1")
    vid = versions.create_version(db, pid, rev="B")
    catrepo.replace_sell_tiers(db, pid, [{"break_qty": 1, "unit_price": 2.0}])
    lifecycle.retire_part(db, pid)
    lifecycle.delete_part(db, pid)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM parts WHERE id IN (?, ?)", (pid, vid)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM part_suppliers WHERE part_id IN (?, ?)",
                            (pid, vid)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM part_price_tiers WHERE part_id = ?",
                            (pid,)).fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_delete_assembly_removes_own_bom_lines(db):
    c = _comp(db, "C-1")
    asm = asmrepo.create_assembly(db, {"part_no": "ASM-1"})
    asmrepo.add_bom_line(db, asm, c, 2, None)
    lifecycle.retire_part(db, asm)
    lifecycle.delete_part(db, asm)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM bom_lines WHERE parent_id = ?", (asm,)).fetchone()[0] == 0
    assert catrepo.get_part(db, c) is not None          # the child survives


def test_delete_blocked_by_stock_movements(db):
    pid = _comp(db, "R-1", qty=5)                         # opening stock = a movement
    lifecycle.retire_part(db, pid)
    with pytest.raises(ValueError, match=r"Cannot delete R-1: 1 stock movements\."):
        lifecycle.delete_part(db, pid)
    assert catrepo.get_part(db, pid) is not None


def test_delete_blocked_by_parent_bom(db):
    c = _comp(db, "C-1")
    asm = asmrepo.create_assembly(db, {"part_no": "ASM-1"})
    asmrepo.add_bom_line(db, asm, c, 1, None)
    lifecycle.retire_part(db, c)
    with pytest.raises(ValueError, match=r"used in 1 assemblies"):
        lifecycle.delete_part(db, c)


def test_delete_blocked_by_work_order_and_lines(db):
    c = _comp(db, "C-1", qty=10)
    asm = asmrepo.create_assembly(db, {"part_no": "ASM-1"})
    asmrepo.add_bom_line(db, asm, c, 1, None)
    worepo.create_work_order(db, {"assembly_id": asm, "qty": 1})
    lifecycle.retire_part(db, asm)
    with pytest.raises(ValueError, match=r"1 work orders"):
        lifecycle.delete_part(db, asm)
    with db.connect() as conn:
        blockers = lifecycle.delete_blockers(conn, c)
    assert blockers["work order lines"] == 1 and blockers["assemblies using it"] == 1


def test_delete_blocked_by_customer_order_line(db):
    pid = _comp(db, "W-1")
    cust = conrepo.create_contact(db, {"kind": "customer", "name": "Acme"})
    oid = corepo.create_order(db, {"customer_id": cust})
    corepo.add_line(db, oid, pid, 2, 1.0, None)
    lifecycle.retire_part(db, pid)
    with pytest.raises(ValueError, match=r"1 customer order lines"):
        lifecycle.delete_part(db, pid)


def test_delete_blocked_by_purchase_order_line(db):
    pid = _comp(db, "R-1")
    sup = conrepo.create_contact(db, {"kind": "supplier", "name": "X"})
    po = porepo.create_po(db, {"supplier_id": sup})
    porepo.add_line(db, po, pid, 10, 0.5)
    lifecycle.retire_part(db, pid)
    with pytest.raises(ValueError, match=r"1 purchase order lines"):
        lifecycle.delete_part(db, pid)


def test_delete_blocked_by_sibling_version_history(db):
    """History on version A blocks deleting via version B (the group is one item)."""
    pid = _comp(db, "R-1", qty=3)
    vid = versions.create_version(db, pid, rev="B")      # no stock, no history of its own
    lifecycle.retire_part(db, vid)
    with pytest.raises(ValueError, match=r"stock movements"):
        lifecycle.delete_part(db, vid)
    assert catrepo.get_part(db, pid) is not None and catrepo.get_part(db, vid) is not None


def test_blockers_ignore_absent_tables(catalog_only):
    pid = _comp(catalog_only, "R-1")
    with catalog_only.connect() as conn:
        assert lifecycle.delete_blockers(conn, pid) == {}
    lifecycle.retire_part(catalog_only, pid)
    lifecycle.delete_part(catalog_only, pid)
    assert catrepo.get_part(catalog_only, pid) is None


def test_format_blockers_lists_only_nonzero():
    text = lifecycle.format_blockers(
        "R-1", {"work orders": 3, "stock movements": 12, "assemblies using it": 2})
    assert text == "Cannot delete R-1: 3 work orders, 12 stock movements, used in 2 assemblies."
