"""Phase 1 tests for part/assembly versioning (a version IS a parts row)."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

import partpilot.web.app as web_app
from partpilot.web.core import FeatureRegistry
from partpilot.web.core.db import Database
from partpilot.web.features.assemblies import feature as assemblies_feature
from partpilot.web.features.assemblies import repo as asm_repo
from partpilot.web.features.catalog import feature as catalog_feature
from partpilot.web.features.contacts import feature as contacts_feature
from partpilot.web.features.catalog import repo, versions
from partpilot.web.features.catalog import stock_index


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "v.db")
    reg = FeatureRegistry()
    reg.register(catalog_feature, contacts_feature)      # catalog first (owns parts), then assemblies (bom_lines)
    reg.register(assemblies_feature)
    database.apply_migrations(reg)
    return database


def _mk_part(db, part_no="98-00001-1", rev="A", qty=10.0):
    return repo.create_part(
        db,
        part={"part_no": part_no, "value": "WidgetA", "rev": rev, "min_qty": 0,
              "sell_tiers": [{"break_qty": 1, "unit_price": 0.5}]},
        supplier_lines=[{"supplier_name": "DK", "supplier_pno": "DK-1", "unit_price": 0.1,
                         "reel_qty": 1000, "moq": 1, "lead_time": 3, "is_default": True,
                         "cost_tiers": [{"break_qty": 1, "unit_price": 0.1}]}],
        opening={"qty": qty},
    )


# ---- migration / invariants ----

def test_new_part_is_released_head_of_its_own_group(db):
    pid = _mk_part(db)
    with db.connect() as c:
        row = c.execute(
            "SELECT version_group_id, version_status FROM parts WHERE id=?", (pid,)
        ).fetchone()
    assert row["version_group_id"] == pid
    assert row["version_status"] == "released"


def test_only_one_released_per_group(db):
    pid = _mk_part(db)
    with db.connect() as c:
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(
                "INSERT INTO parts (part_no, version_group_id, version_status) "
                "VALUES ('98-00001-1', ?, 'released')", (pid,),
            )
            c.commit()


def test_draft_and_superseded_coexist(db):
    pid = _mk_part(db)
    with db.connect() as c:  # two non-released rows in the same group are fine
        c.execute("INSERT INTO parts (part_no, version_group_id, version_status) "
                  "VALUES ('98-00001-1', ?, 'draft')", (pid,))
        c.execute("INSERT INTO parts (part_no, version_group_id, version_status) "
                  "VALUES ('98-00001-1', ?, 'superseded')", (pid,))
        c.commit()
        n = c.execute("SELECT COUNT(*) FROM parts WHERE version_group_id=?", (pid,)).fetchone()[0]
    assert n == 3


# ---- create_version ----

def test_create_version_clones_part_attributes_but_not_stock(db):
    pid = _mk_part(db, qty=25.0)
    vid = versions.create_version(db, pid, rev="B")
    with db.connect() as c:
        src = dict(c.execute("SELECT * FROM parts WHERE id=?", (pid,)).fetchone())
        new = dict(c.execute("SELECT * FROM parts WHERE id=?", (vid,)).fetchone())
    assert new["part_no"] == src["part_no"]                 # same number
    assert new["version_group_id"] == src["version_group_id"]
    assert new["rev"] == "B"
    assert new["version_status"] == "draft"                 # new versions start as draft
    assert new["value"] == src["value"]                     # attributes copied
    assert new["total_qty"] == 0                            # stock NOT copied
    assert new["minimrp_id"] is None                        # UNIQUE key not copied


def test_create_version_copies_suppliers_and_sell_tiers_but_not_stock_rows(db):
    pid = _mk_part(db)
    vid = versions.create_version(db, pid, rev="B")
    with db.connect() as c:
        sup = c.execute("SELECT COUNT(*) FROM part_suppliers WHERE part_id=?", (vid,)).fetchone()[0]
        tiers = c.execute(
            "SELECT COUNT(*) FROM part_supplier_tiers t JOIN part_suppliers ps ON ps.id=t.part_supplier_id "
            "WHERE ps.part_id=?", (vid,)).fetchone()[0]
        sell = c.execute("SELECT COUNT(*) FROM part_price_tiers WHERE part_id=?", (vid,)).fetchone()[0]
        stock_rows = c.execute("SELECT COUNT(*) FROM part_stock WHERE part_id=?", (vid,)).fetchone()[0]
    assert sup == 1 and tiers == 1 and sell == 1
    assert stock_rows == 0            # a fresh version owns no stock yet


def test_create_version_deep_copies_bom_lines(db):
    child = _mk_part(db, part_no="R-100", rev="")
    asm = asm_repo.create_assembly(db, {"part_no": "98-00050-1", "value": "Board", "rev": "A"})
    asm_repo.add_bom_line(db, asm, child, 3, "R1,R2,R3")
    vid = versions.create_version(db, asm, rev="B")
    with db.connect() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT parent_id, child_id, qty_per, refdes FROM bom_lines WHERE parent_id=?", (vid,))]
    assert len(rows) == 1
    assert rows[0]["child_id"] == child          # child pinned to the same version
    assert rows[0]["qty_per"] == 3
    assert rows[0]["refdes"] == "R1,R2,R3"


# ---- release_version ----

def test_release_promotes_and_supersedes_prior(db):
    pid = _mk_part(db)
    vid = versions.create_version(db, pid, rev="B")
    assert versions.current_version_id(db, pid) == pid       # released stays pid while B is draft
    versions.release_version(db, vid)
    with db.connect() as c:
        rows = {r["id"]: r["version_status"]
                for r in c.execute("SELECT id, version_status FROM parts WHERE version_group_id=?", (pid,))}
    assert rows[pid] == "superseded"
    assert rows[vid] == "released"
    assert versions.current_version_id(db, pid) == vid


def test_release_is_idempotent_and_reactivates_old(db):
    pid = _mk_part(db)
    vid = versions.create_version(db, pid, rev="B")
    versions.release_version(db, vid)
    versions.release_version(db, vid)                         # no-op
    assert versions.current_version_id(db, pid) == vid
    versions.release_version(db, pid)                         # re-activate the old rev
    with db.connect() as c:
        released = [r["id"] for r in c.execute(
            "SELECT id FROM parts WHERE version_group_id=? AND version_status='released'", (pid,))]
    assert released == [pid]                                  # exactly one, and it's the reactivated one


# ---- resolve-by-number ----

def test_current_version_for_part_no_prefers_released(db):
    pid = _mk_part(db)
    vid = versions.create_version(db, pid, rev="B")           # draft
    got = versions.current_version_for_part_no(db, "98-00001-1")
    assert got["id"] == pid                                   # released wins over the draft
    versions.release_version(db, vid)
    got2 = versions.current_version_for_part_no(db, "98-00001-1")
    assert got2["id"] == vid                                  # now B is current


# ---- phase 2: resolution correctness ----

def test_list_parts_collapses_to_current_version(db):
    pid = _mk_part(db, part_no="R-1", rev="A")
    vid = versions.create_version(db, pid, rev="B")          # draft in same group
    rows, total = repo.list_parts(db)
    ids = [r["id"] for r in rows]
    assert total == 1 and ids == [pid]                       # one row, the released one
    versions.release_version(db, vid)                        # B now current
    rows2, total2 = repo.list_parts(db)
    assert total2 == 1 and [r["id"] for r in rows2] == [vid]
    assert rows2[0]["version_count"] == 2


def test_list_assemblies_collapses_to_current_version(db):
    asm = asm_repo.create_assembly(db, {"part_no": "98-1", "value": "A", "rev": "A"})
    v = versions.create_version(db, asm, rev="B")
    rows = asm_repo.list_assemblies(db)
    ids = [r["id"] for r in rows]
    assert ids == [asm] and rows[0]["version_count"] == 2
    versions.release_version(db, v)
    assert [r["id"] for r in asm_repo.list_assemblies(db)] == [v]


def test_find_by_part_no_and_mpn_prefer_released(db):
    pid = _mk_part(db, part_no="M-1", rev="A")
    vid = versions.create_version(db, pid, rev="B")
    assert repo.find_part_by_part_no(db, "M-1")["id"] == pid  # released, not the draft
    assert repo.find_part_id_by_mpn(db, "M-1") == pid
    versions.release_version(db, vid)
    assert repo.find_part_by_part_no(db, "M-1")["id"] == vid
    assert repo.find_part_id_by_mpn(db, "M-1") == vid


def test_stock_index_excludes_non_released_versions(db):
    pid = _mk_part(db, part_no="GRM-1", rev="A", qty=100.0)
    vid = versions.create_version(db, pid, rev="B")          # a draft, zero stock
    all_ids = {it.item_id for it in stock_index.build_stock_index(db).items}
    assert pid in all_ids and vid not in all_ids


def test_pickers_show_only_current_version(db):
    asm = asm_repo.create_assembly(db, {"part_no": "98-9", "value": "Board"})
    child = _mk_part(db, part_no="C-1", rev="A")
    cdraft = versions.create_version(db, child, rev="B")     # draft child
    picker_ids = {p["id"] for p in asm_repo.parts_for_picker(db, asm)}
    assert child in picker_ids and cdraft not in picker_ids


# ---- web routes ----

@pytest.fixture
def app(tmp_path):
    application = web_app.create_app(
        db_path=tmp_path / "partpilot.db", data_dir=tmp_path / "data", secret_key="test-secret")
    store = application.state.store
    store.create_user("buyer1", "pw", role="purchasing")
    store.create_user("ware1", "pw", role="warehouse")
    return application


def _login(client, username, password):
    return client.post("/login", data={"username": username, "password": password},
                       follow_redirects=False)


def test_part_detail_renders_versions_panel(app):
    pid = _mk_part(app.state.database)
    client = TestClient(app)
    _login(client, "buyer1", "pw")
    r = client.get(f"/catalog/{pid}")
    assert r.status_code == 200
    assert "Versions" in r.text and "New version" in r.text


def test_part_panel_form_action_is_catalog_prefixed(app):
    pid = _mk_part(app.state.database)
    client = TestClient(app)
    _login(client, "buyer1", "pw")
    r = client.get(f"/catalog/{pid}")
    assert f'action="/catalog/{pid}/versions/new"' in r.text


def test_assembly_panel_form_action_is_assemblies_prefixed(app):
    # Regression: the assembly page must pass version_base so the shared panel's form action carries
    # the /assemblies prefix — a missing base rendered "/{id}/versions/new" and 404'd.
    asm = asm_repo.create_assembly(app.state.database, {"part_no": "06-00455-1", "value": "Board"})
    client = TestClient(app)
    _login(client, "buyer1", "pw")
    r = client.get(f"/assemblies/{asm}")
    assert r.status_code == 200
    assert f'action="/assemblies/{asm}/versions/new"' in r.text
    assert f'action="/{asm}/versions/new"' not in r.text        # the bug we fixed


def test_new_version_route_redirects_to_draft_edit(app):
    pid = _mk_part(app.state.database)
    client = TestClient(app)
    _login(client, "buyer1", "pw")
    r = client.post(f"/catalog/{pid}/versions/new", data={"rev": "B"}, follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.endswith("/edit") and "/catalog/" in loc
    new_id = int(loc.split("/catalog/")[1].split("/")[0])
    assert new_id != pid
    # release it, current flips
    rel = client.post(f"/catalog/{new_id}/versions/release", follow_redirects=False)
    assert rel.status_code == 303
    assert versions.current_version_id(app.state.database, pid) == new_id


def test_version_routes_are_role_gated(app):
    pid = _mk_part(app.state.database)
    client = TestClient(app)
    _login(client, "ware1", "pw")               # warehouse: not a catalog writer
    r = client.post(f"/catalog/{pid}/versions/new", data={"rev": "B"}, follow_redirects=False)
    assert r.status_code == 403


def test_stock_index_excludes_retired_parts(db):
    from partpilot.web.features.catalog import lifecycle
    pid = _mk_part(db, part_no="GRM-9", rev="A", qty=100.0)
    assert pid in {it.item_id for it in stock_index.build_stock_index(db).items}
    lifecycle.retire_part(db, pid)
    assert pid not in {it.item_id for it in stock_index.build_stock_index(db).items}
