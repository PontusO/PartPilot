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
from digisearch.web.features.customer_orders import repo as corepo
from digisearch.web.features.planning import feature as planning_feature
from digisearch.web.features.planning import repo
from digisearch.web.features.work_orders import feature as wo_feature
from digisearch.web.features.work_orders import repo as worepo


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "pl.db")
    reg = FeatureRegistry()
    reg.register(catalog_feature, assemblies_feature, contacts_feature, co_feature,
                 wo_feature, planning_feature)
    database.apply_migrations(reg)
    return database


def test_calendar_has_build_bar_and_purchasing_marker(db):
    comp = catrepo.create_part(db, part={"part_no": "PC-1"},
                               supplier_lines=[{"supplier_name": "X", "unit_price": 1,
                                                "reel_qty": 1, "is_default": True}])
    with db.connect() as c:
        c.execute("UPDATE part_suppliers SET lead_time = 7 WHERE part_id = ?", (comp,))
        c.commit()
    asm = asmrepo.create_assembly(db, {"part_no": "PA-1", "default_build_days": 5})
    asmrepo.add_bom_line(db, asm, comp, 2, None)
    cust = conrepo.create_contact(db, {"kind": "customer", "name": "Acme"})
    oid = corepo.create_order(db, {"customer_id": cust, "order_ref": "SO-1",
                                   "required_date": "2026-07-31"})
    corepo.add_line(db, oid, asm, 4, 100.0, None)
    line_id = corepo.get_order(db, oid)["lines"][0]["id"]
    wo_id = worepo.create_work_orders_for_order(db, oid, {line_id: 4}, "u")[0]

    events = repo.calendar_events(db)
    types = [e["extendedProps"]["type"] for e in events]
    assert "wo" in types and "buyby" in types
    assert "due" not in types  # the customer's requested date is no longer a calendar marker

    wo_ev = next(e for e in events if e["extendedProps"]["type"] == "wo")
    assert wo_ev["color"] == "#8b5cf6" and wo_ev["editable"] is True   # purple build bar, draggable
    assert "due 2026-07-31" in wo_ev["title"]                          # due date in the label

    buy_ev = next(e for e in events if e["extendedProps"]["type"] == "buyby")
    assert buy_ev["extendedProps"]["wo_id"] == wo_id and buy_ev["editable"] is True
    assert buy_ev["start"] == worepo.get_work_order(db, wo_id)["purchase_by"]


def test_diverged_wo_is_marked_on_calendar(db):
    comp = catrepo.create_part(db, part={"part_no": "DC-1"},
                               supplier_lines=[{"supplier_name": "X", "unit_price": 1,
                                                "reel_qty": 1, "is_default": True}])
    asm = asmrepo.create_assembly(db, {"part_no": "DA-1", "default_build_days": 5})
    asmrepo.add_bom_line(db, asm, comp, 2, None)
    wo_id = worepo.create_work_order(db, {"assembly_id": asm, "qty": 1, "due_date": "2026-07-31"})

    wo_ev = next(e for e in repo.calendar_events(db) if e["extendedProps"]["type"] == "wo")
    assert wo_ev["extendedProps"]["bom_diverged"] is False and wo_ev["color"] == "#8b5cf6"

    # Edit the assembly BOM after planning → the build bar is flagged (amber, ⚠ in the title).
    extra = catrepo.create_part(db, part={"part_no": "DC-2"},
                                supplier_lines=[{"supplier_name": "X", "unit_price": 1,
                                                 "reel_qty": 1, "is_default": True}])
    asmrepo.add_bom_line(db, asm, extra, 1, None)
    wo_ev = next(e for e in repo.calendar_events(db) if e["extendedProps"]["type"] == "wo")
    assert wo_ev["extendedProps"]["bom_diverged"] is True
    assert wo_ev["color"] == "#d97706" and wo_ev["title"].startswith("⚠ BOM changed")


def test_unscheduled_and_cancelled_wos_are_absent(db):
    asm = asmrepo.create_assembly(db, {"part_no": "PA-2"})
    comp = catrepo.create_part(db, part={"part_no": "PC-2"},
                               supplier_lines=[{"supplier_name": "X", "unit_price": 1,
                                                "reel_qty": 1, "is_default": True}])
    asmrepo.add_bom_line(db, asm, comp, 1, None)
    wo_id = worepo.create_work_order(db, {"assembly_id": asm, "qty": 1})  # no due/duration → unscheduled
    worepo.cancel_work_order(db, wo_id)
    assert repo.calendar_events(db) == []  # nothing to show
