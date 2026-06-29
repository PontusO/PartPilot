import pytest

from digisearch.web.core import FeatureRegistry
from digisearch.web.core.db import Database
from digisearch.web.features.assemblies import feature as assemblies_feature
from digisearch.web.features.assemblies import repo as asmrepo
from digisearch.web.features.catalog import feature as catalog_feature
from digisearch.web.features.catalog import repo as catrepo
from digisearch.web.features.catalog import stock
from digisearch.web.features.contacts import feature as contacts_feature
from digisearch.web.features.contacts import repo as conrepo
from digisearch.web.features.customer_orders import feature as co_feature
from digisearch.web.features.customer_orders import repo as corepo
from digisearch.web.features.work_orders import feature as wo_feature
from digisearch.web.features.work_orders import repo


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "wo.db")
    reg = FeatureRegistry()
    reg.register(catalog_feature, assemblies_feature, contacts_feature, co_feature, wo_feature)
    database.apply_migrations(reg)
    return database


def _comp(db, part_no, qty):
    return catrepo.create_part(
        db, part={"part_no": part_no},
        supplier_lines=[{"supplier_name": "X", "unit_price": 1, "reel_qty": 1, "is_default": True}],
        opening={"qty": qty})


def _setup(db):
    """TOP-1 = 3×SUB-1 + 5×COMP-B; SUB-1 = 2×COMP-A. Stock: A=100, B=30."""
    a = _comp(db, "COMP-A", 100)
    b = _comp(db, "COMP-B", 30)
    sub = asmrepo.create_assembly(db, {"part_no": "SUB-1"})
    asmrepo.add_bom_line(db, sub, a, 2, None)
    top = asmrepo.create_assembly(db, {"part_no": "TOP-1"})
    asmrepo.add_bom_line(db, top, sub, 3, None)
    asmrepo.add_bom_line(db, top, b, 5, None)
    return a, b, sub, top


def test_explode_multilevel(db):
    a, b, sub, top = _setup(db)
    with db.connect() as conn:
        ex = repo.explode_to_components(conn, top, 10)
    assert ex == {a: 60.0, b: 50.0}  # 3×2×10 of A, 5×10 of B; SUB exploded away to base parts


def test_work_order_lifecycle_moves_stock(db):
    a, b, sub, top = _setup(db)
    wo = repo.create_work_order(db, {"wo_no": "WO-1", "assembly_id": top, "qty": 10})
    w = repo.get_work_order(db, wo)
    assert w["status"] == "allocated"
    req = {ln["part_no"]: ln for ln in w["lines"]}
    assert req["COMP-A"]["qty_required"] == 60 and req["COMP-B"]["qty_required"] == 50
    assert w["short_count"] == 1  # B short: need 50, have 30

    repo.issue_work_order(db, wo, "u")
    assert repo.get_work_order(db, wo)["status"] == "issued"
    assert catrepo.get_part(db, a)["total_qty"] == 40    # 100 − 60
    assert catrepo.get_part(db, b)["total_qty"] == -20   # 30 − 50 (negative allowed, like miniMRP)

    repo.finish_work_order(db, wo, "u")
    assert repo.get_work_order(db, wo)["status"] == "finished"
    assert catrepo.get_part(db, top)["total_qty"] == 10  # finished assemblies into stock

    assert any(m["mtype"] == "ISSUE" for m in stock.movements_for_part(db, a))
    assert any(m["mtype"] == "BUILD" for m in stock.movements_for_part(db, top))


def test_cannot_finish_before_issue(db):
    _, _, _, top = _setup(db)
    wo = repo.create_work_order(db, {"assembly_id": top, "qty": 1})
    with pytest.raises(ValueError):
        repo.finish_work_order(db, wo)


def test_cancel_only_when_allocated(db):
    _, _, _, top = _setup(db)
    wo = repo.create_work_order(db, {"assembly_id": top, "qty": 1})
    repo.cancel_work_order(db, wo)
    assert repo.get_work_order(db, wo)["status"] == "cancelled"

    wo2 = repo.create_work_order(db, {"assembly_id": top, "qty": 1})
    repo.issue_work_order(db, wo2)
    with pytest.raises(ValueError):
        repo.cancel_work_order(db, wo2)  # already issued — stock moved


def test_flush_straight_through(db):
    a, b, sub, top = _setup(db)
    wo = repo.create_work_order(db, {"assembly_id": top, "qty": 2})
    repo.flush_work_order(db, wo, "u")
    assert repo.get_work_order(db, wo)["status"] == "finished"
    assert catrepo.get_part(db, top)["total_qty"] == 2
    assert catrepo.get_part(db, a)["total_qty"] == 100 - 12  # 3×2×2 of A


def test_fulfilment_proposals_and_linked_wo(db):
    a, b, sub, top = _setup(db)  # TOP-1 buildable assembly (0 in stock), COMP-A a component
    cust = conrepo.create_contact(db, {"kind": "customer", "name": "Acme"})
    oid = corepo.create_order(db, {"customer_id": cust})
    corepo.add_line(db, oid, top, 5, 1.0, None)   # 5× assembly TOP-1  -> short
    corepo.add_line(db, oid, a, 10, 1.0, None)    # 10× component COMP-A -> purchase, not build

    props = {p["part_no"]: p for p in repo.fulfilment_proposals(db, oid)}
    assert props["TOP-1"]["category"] == "build" and props["TOP-1"]["shortfall"] == 5
    assert props["COMP-A"]["category"] == "component"

    top_line = next(p["line_id"] for p in repo.fulfilment_proposals(db, oid) if p["part_no"] == "TOP-1")
    created = repo.create_work_orders_for_order(db, oid, {top_line: 5}, "u")
    assert len(created) == 1
    wo = repo.get_work_order(db, created[0])
    assert wo["customer_order_line_id"] == top_line and wo["qty"] == 5

    # the line is now covered by the open WO and shows as linked
    props2 = {p["part_no"]: p for p in repo.fulfilment_proposals(db, oid)}
    assert props2["TOP-1"]["category"] == "covered" and props2["TOP-1"]["on_wo"] == 5
    assert repo.work_orders_for_order(db, oid)[top_line][0]["id"] == created[0]


def test_build_to_fulfil_auto_plans_schedule(db):
    from digisearch.web.core import iso, sub_workdays

    _, _, _, top = _setup(db)
    with db.connect() as conn:
        conn.execute("UPDATE parts SET default_build_days = 5 WHERE id = ?", (top,))
        conn.commit()
    cust = conrepo.create_contact(db, {"kind": "customer", "name": "Acme"})
    oid = corepo.create_order(db, {"customer_id": cust, "required_date": "2026-07-31"})
    corepo.add_line(db, oid, top, 3, 100.0, None)
    line_id = corepo.get_order(db, oid)["lines"][0]["id"]

    wo_id = repo.create_work_orders_for_order(db, oid, {line_id: 3}, "u")[0]
    wo = repo.get_work_order(db, wo_id)
    assert wo["due_date"] == "2026-07-31"           # due = customer required date
    assert wo["duration_days"] == 5                 # from the assembly default
    assert wo["planned_start"] == iso(sub_workdays("2026-07-31", 5))  # back-scheduled (working days)


def test_reschedule_move_and_resize(db):
    from digisearch.web.core import add_workdays, iso, workdays_between

    _, _, _, top = _setup(db)
    wo_id = repo.create_work_order(db, {"assembly_id": top, "qty": 1,
                                        "due_date": "2026-07-31", "duration_days": 4})
    assert repo.get_work_order(db, wo_id)["due_date"] == "2026-07-31"

    repo.reschedule_work_order(db, wo_id, planned_start="2026-08-03")   # move: keep duration
    wo = repo.get_work_order(db, wo_id)
    assert wo["planned_start"] == "2026-08-03" and wo["duration_days"] == 4
    assert wo["due_date"] == iso(add_workdays("2026-08-03", 4))         # due recomputed

    repo.reschedule_work_order(db, wo_id, planned_start="2026-08-03", due_date="2026-08-14")  # resize
    wo = repo.get_work_order(db, wo_id)
    assert wo["due_date"] == "2026-08-14"
    assert wo["duration_days"] == workdays_between("2026-08-03", "2026-08-14")  # duration recomputed


def test_reschedule_is_due_driven(db):
    from digisearch.web.core import iso, sub_workdays

    _, _, _, top = _setup(db)
    wo_id = repo.create_work_order(db, {"assembly_id": top, "qty": 1,
                                        "due_date": "2026-07-31", "duration_days": 5})
    repo.reschedule_work_order(db, wo_id, due_date="2026-08-31", duration_days=5)  # change the due date
    wo = repo.get_work_order(db, wo_id)
    assert wo["due_date"] == "2026-08-31"
    assert wo["planned_start"] == iso(sub_workdays("2026-08-31", 5))   # start back-scheduled from due


def test_rescheduling_retains_customer_requested_date(db):
    _, _, _, top = _setup(db)
    with db.connect() as conn:
        conn.execute("UPDATE parts SET default_build_days = 5 WHERE id = ?", (top,))
        conn.commit()
    cust = conrepo.create_contact(db, {"kind": "customer", "name": "Acme"})
    oid = corepo.create_order(db, {"customer_id": cust, "required_date": "2026-07-31"})
    corepo.add_line(db, oid, top, 2, 100.0, None)
    line_id = corepo.get_order(db, oid)["lines"][0]["id"]
    wo_id = repo.create_work_orders_for_order(db, oid, {line_id: 2}, "u")[0]

    wo = repo.get_work_order(db, wo_id)
    assert wo["customer_required_date"] == "2026-07-31" and wo["due_date"] == "2026-07-31"

    repo.reschedule_work_order(db, wo_id, due_date="2026-09-30", duration_days=5)  # move the plan later
    wo = repo.get_work_order(db, wo_id)
    assert wo["due_date"] == "2026-09-30"                       # planning date moved
    assert wo["customer_required_date"] == "2026-07-31"         # customer's request retained
    assert corepo.get_order(db, oid)["required_date"] == "2026-07-31"


def test_default_build_days_is_five(db):
    from digisearch.web.core import iso, sub_workdays

    _, _, _, top = _setup(db)  # TOP-1 has no default_build_days
    wo_id = repo.create_work_order(db, {"assembly_id": top, "qty": 1, "due_date": "2026-07-31"})
    wo = repo.get_work_order(db, wo_id)
    assert wo["duration_days"] == 5                                    # global fallback
    assert wo["planned_start"] == iso(sub_workdays("2026-07-31", 5))


def test_purchase_by_auto_set_and_draggable(db):
    from digisearch.web.core import iso, sub_workdays

    a, _, _, top = _setup(db)
    with db.connect() as conn:
        conn.execute("UPDATE parts SET default_build_days = 5 WHERE id = ?", (top,))
        conn.execute("UPDATE part_suppliers SET lead_time = 10 WHERE part_id = ?", (a,))  # COMP-A
        conn.commit()
    wo_id = repo.create_work_order(db, {"assembly_id": top, "qty": 1, "due_date": "2026-07-31"})
    wo = repo.get_work_order(db, wo_id)
    assert wo["purchase_by"] == iso(sub_workdays(wo["planned_start"], 10))   # from the lead time

    repo.reschedule_work_order(db, wo_id, due_date="2026-09-30", duration_days=5)  # move the build
    wo = repo.get_work_order(db, wo_id)
    assert wo["purchase_by"] == iso(sub_workdays(wo["planned_start"], 10))   # re-derived from new start

    repo.set_purchase_by(db, wo_id, "2026-08-15")                            # drag it earlier
    assert repo.get_work_order(db, wo_id)["purchase_by"] == "2026-08-15"


def test_buy_by_uses_component_lead_time(db):
    from digisearch.web.core import iso, sub_workdays

    a, _, _, top = _setup(db)
    with db.connect() as conn:
        conn.execute("UPDATE part_suppliers SET lead_time = 10 WHERE part_id = ?", (a,))  # COMP-A
        conn.commit()
    wo_id = repo.create_work_order(db, {"assembly_id": top, "qty": 1,
                                        "due_date": "2026-07-31", "duration_days": 5})
    wo = repo.get_work_order(db, wo_id)
    bb = repo.buy_by_for_wo(db, wo_id)
    assert bb is not None
    assert bb["critical"] == iso(sub_workdays(wo["planned_start"], 10))
    assert any(ln["part_no"] == "COMP-A" and ln["lead_time"] == 10 for ln in bb["lines"])


def test_spillage_margin_inflates_requirements(db):
    a, _, _, top = _setup(db)  # TOP-1 → COMP-A 6/build, COMP-B 5/build
    with db.connect() as conn:  # set a 10% global spillage
        conn.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO app_settings (key, value) VALUES ('production.spillage_percent', '10')")
        conn.commit()

    wo = repo.get_work_order(db, repo.create_work_order(db, {"assembly_id": top, "qty": 10}))
    req = {ln["part_no"]: ln["qty_required"] for ln in wo["lines"]}
    assert req["COMP-A"] == 66 and req["COMP-B"] == 55   # 60/50 + 10%
    assert wo["spillage_percent"] == 10.0

    wo2 = repo.get_work_order(db, repo.create_work_order(db, {"assembly_id": top, "qty": 1}))
    req2 = {ln["part_no"]: ln["qty_required"] for ln in wo2["lines"]}
    assert req2["COMP-A"] == 7 and req2["COMP-B"] == 6   # 6.6→7, 5.5→6 rounded up to whole parts


def test_min_margin_qty_floors_the_spillage(db):
    a, _, _, top = _setup(db)  # TOP-1 → COMP-A 6/build, COMP-B 5/build
    with db.connect() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO app_settings (key, value) VALUES ('production.spillage_percent', '3')")
        conn.execute("INSERT INTO app_settings (key, value) VALUES ('production.min_margin_qty', '15')")
        conn.commit()

    # small batch: 3% (1.8/1.5) is below the 15 minimum → +15 floor wins
    wo = repo.get_work_order(db, repo.create_work_order(db, {"assembly_id": top, "qty": 10}))
    req = {ln["part_no"]: ln["qty_required"] for ln in wo["lines"]}
    assert req["COMP-A"] == 75 and req["COMP-B"] == 65   # 60+15, 50+15
    assert wo["spillage_percent"] == 3.0 and wo["min_margin_qty"] == 15.0

    # large batch: the 3% exceeds the minimum → percentage wins
    wo2 = repo.get_work_order(db, repo.create_work_order(db, {"assembly_id": top, "qty": 1000}))
    req2 = {ln["part_no"]: ln["qty_required"] for ln in wo2["lines"]}
    assert req2["COMP-A"] == 6180 and req2["COMP-B"] == 5150   # 6000+180, 5000+150


def test_no_spillage_leaves_requirements_unchanged(db):
    _, _, _, top = _setup(db)  # no app_settings table → spillage 0
    wo = repo.get_work_order(db, repo.create_work_order(db, {"assembly_id": top, "qty": 10}))
    req = {ln["part_no"]: ln["qty_required"] for ln in wo["lines"]}
    assert req["COMP-A"] == 60 and req["COMP-B"] == 50 and (wo["spillage_percent"] or 0) == 0


def test_unlimited_stock_part_never_short_and_not_consumed(db):
    a, b, sub, top = _setup(db)
    # A labour line (e.g. SMT Assembly) with unlimited stock, added to TOP-1's BOM.
    labour = catrepo.create_part(
        db, part={"part_no": "SMT-ASSY", "unlimited_stock": 1},
        supplier_lines=[{"supplier_name": "X", "unit_price": 2.5, "reel_qty": 1, "is_default": True}],
        opening={"qty": 0})
    asmrepo.add_bom_line(db, top, labour, 1, None)

    wo = repo.create_work_order(db, {"assembly_id": top, "qty": 10})
    w = repo.get_work_order(db, wo)
    labour_line = next(ln for ln in w["lines"] if ln["part_no"] == "SMT-ASSY")
    assert labour_line["unlimited"] is True
    assert labour_line["short"] == 0           # unlimited -> never short even at 0 on hand
    # only COMP-B is short; the unlimited labour line is not counted
    assert w["short_count"] == 1

    repo.issue_work_order(db, wo, "u")
    assert catrepo.get_part(db, labour)["total_qty"] == 0   # not consumed from stock
    assert not stock.movements_for_part(db, labour)         # no ISSUE movement posted
    assert catrepo.get_part(db, a)["total_qty"] == 40       # ordinary parts still consumed


def test_unlimited_stock_part_never_below_min(db):
    pid = catrepo.create_part(
        db, part={"part_no": "LABOUR", "min_qty": 100, "unlimited_stock": 1},
        supplier_lines=[{"supplier_name": "X", "unit_price": 1, "reel_qty": 1, "is_default": True}],
        opening={"qty": 0})
    parts, _ = catrepo.list_parts(db)
    row = next(p for p in parts if p["id"] == pid)
    assert row["unlimited"] is True and row["below_min"] is False
    assert catrepo.summary(db)["below_min"] == 0   # excluded from the below-min count


def test_only_assemblies_with_bom_are_buildable(db):
    _setup(db)
    catrepo.create_part(db, part={"part_no": "LONE"},
                        supplier_lines=[{"supplier_name": "X", "unit_price": 1, "reel_qty": 1,
                                         "is_default": True}])
    asmrepo.create_assembly(db, {"part_no": "EMPTY-ASSY"})  # no BOM lines
    names = {x["part_no"] for x in repo.assemblies(db)}
    assert "TOP-1" in names and "SUB-1" in names
    assert "EMPTY-ASSY" not in names and "LONE" not in names
