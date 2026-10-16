# Part Lifecycle (Retire → Delete) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let admin/purchasing retire any part or assembly (hidden from lists and pickers, never bought or built again, stock may run out), and let admins delete a retired part that nothing historical references.

**Architecture:** One additive column `parts.retired_at` (catalog migration v24) and a new `catalog/lifecycle.py` module own retire/unretire/delete for a whole version group, with a blocker scan over every table that references `parts`. Every picker and shortage/resolver query gains `AND p.retired_at IS NULL`; write paths that take a part id gain a server-side refusal. Catalog and assemblies routers expose the same three POST actions and render refusals in `.err`.

**Tech Stack:** FastAPI, raw sqlite3 via `Database`, Jinja templates, pytest.

**Spec:** `docs/superpowers/specs/2026-10-16-part-lifecycle-design.md`

## Global Constraints

- Never edit a shipped migration; catalog's next version is **24**.
- Feature order in `app.py` is unchanged. Catalog-only test registries exist: every lifecycle query that touches another feature's table must check `table_exists` first.
- Roles: retire/unretire = `frozenset({"admin", "purchasing"})` (the existing write roles); delete = `frozenset({"admin"})`.
- Lifecycle acts on the **whole version group** (`COALESCE(version_group_id, id)`).
- Refusal copy, verbatim: `Retire <part_no> before deleting it.` and `Cannot delete <part_no>: <n> work orders, <n> stock movements, used in <n> assemblies.` (only non-zero items, comma-separated, labels from the spec table; `bom_lines.child_id` is phrased `used in N assemblies`). Guard copy: `<part_no> is retired and cannot be built.` / `<part_no> is retired and cannot be added to a BOM.` / `<part_no> is retired and cannot be ordered.`
- UI per `docs/design-rules.md`: real `<button>`s in `<form method="post">`, `.err` for refusals, `badge muted` for **Retired**.
- Commit after each task; update `docs/user-manual.md` with UI changes (Task 6 is the docs task; Task 5 commits the manual edits for the new buttons in the same commit).

## Review Focus

1. Deleting a retired part whose *other version* has history: the group scan must look at every version id, not just the clicked one (Task 1 test `test_delete_blocked_by_sibling_version_history`).
2. A retired assembly reached by a stale "New work order" form or a typed id: `create_work_order` refuses regardless of the picker (Task 3).
3. A retired component in a live BOM: work orders still issue it; purchase suggestions never list it (Task 3 + Task 4 tests).
4. The resolver's own-stock pre-check with a retired part holding stock: must not report `IN_STOCK` against it (Task 4 `test_stock_index_excludes_retired_parts`).
5. Part number re-use: a retired part's number must still count as taken by `find_part_by_part_no` (Task 4 `test_find_part_by_part_no_still_sees_retired`).

---

### Task 1: Migration v24 + `catalog/lifecycle.py`

**Files:**
- Modify: `src/partpilot/web/features/catalog/migrations.py` (append v24)
- Create: `src/partpilot/web/features/catalog/lifecycle.py`
- Test: `tests/test_lifecycle.py`

**Interfaces:**
- Produces: `lifecycle.group_ids(conn, part_id) -> list[int]`, `lifecycle.delete_blockers(conn, part_id) -> dict[str, int]`, `lifecycle.retire_part(db, part_id) -> None`, `lifecycle.unretire_part(db, part_id) -> None`, `lifecycle.delete_part(db, part_id) -> None` (raises `ValueError`), `lifecycle.format_blockers(part_no, blockers) -> str`, `lifecycle.is_retired(conn, part_id) -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_lifecycle.py
import pytest

from partpilot.web.core import FeatureRegistry
from partpilot.web.core.db import Database
from partpilot.web.features.assemblies import feature as assemblies_feature
from partpilot.web.features.assemblies import repo as asmrepo
from partpilot.web.features.catalog import feature as catalog_feature
from partpilot.web.features.catalog import lifecycle, repo as catrepo, versions
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
        assert conn.execute("SELECT COUNT(*) FROM part_suppliers WHERE part_id IN (?, ?)", (pid, vid)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM part_price_tiers WHERE part_id = ?", (pid,)).fetchone()[0] == 0
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


def test_blockers_ignore_absent_tables(catalog_only):
    pid = _comp(catalog_only, "R-1")
    with catalog_only.db_connect() if hasattr(catalog_only, "db_connect") else catalog_only.connect() as conn:
        blockers = lifecycle.delete_blockers(conn, pid)
    assert blockers == {}  # every count is zero and absent tables are simply not counted
    lifecycle.retire_part(catalog_only, pid)
    lifecycle.delete_part(catalog_only, pid)
    assert catrepo.get_part(catalog_only, pid) is None


def test_format_blockers_lists_only_nonzero():
    text = lifecycle.format_blockers(
        "R-1", {"work orders": 3, "stock movements": 12, "assemblies using it": 2})
    assert text == "Cannot delete R-1: 3 work orders, 12 stock movements, used in 2 assemblies."
```

Note: `test_blockers_ignore_absent_tables` uses `catalog_only.connect()`; drop the `hasattr` hedge
when writing it — `Database.connect()` is the API. `delete_blockers` returns only non-zero counts.
Check `corepo.create_order` / `porepo.create_po` signatures in their repos before running; if
`create_order` needs more keys (e.g. `status`), copy the minimal dict `tests/test_customer_orders.py`
and `tests/test_purchase_orders.py` use.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_lifecycle.py -q 2>&1 | tail -5`
Expected: FAIL / ERROR with `ImportError: cannot import name 'lifecycle'`.

- [ ] **Step 3: Add migration v24**

Append to `MIGRATIONS` in `src/partpilot/web/features/catalog/migrations.py`:

```python
    Migration(
        version=24,
        name="part lifecycle: retired_at",
        # NULL = active. Set on every version of a group by catalog.lifecycle.retire_part; retired
        # parts leave lists, pickers, purchase suggestions and the resolver's own-stock check but
        # stay resolvable by id so history keeps rendering. Delete is a separate, admin-only step.
        sql="""
        ALTER TABLE parts ADD COLUMN retired_at TEXT;
        CREATE INDEX ix_parts_retired ON parts(retired_at);
        """,
    ),
```

- [ ] **Step 4: Write `lifecycle.py`**

```python
"""Part lifecycle: retire → (admin) delete, for components and assemblies alike.

A retired part (``parts.retired_at`` not NULL) is hidden from lists and pickers, never suggested for
purchase, never matched by the resolver's own-stock check, and cannot be built or added to a BOM —
but everything that already references it keeps rendering, and its stock may still be despatched
("run out, don't restart"). Retiring applies to the whole version group. Deleting is allowed only
once retired and only when no historical record points at any version in the group; the schema's
ON DELETE CASCADE then removes the part's own BOM lines, offers, tiers and stock rows.
"""

from __future__ import annotations

from ...core.db import Database, table_exists

# label → (table, column). Checked only when the table exists (partial test registries).
# stock_movements cascades in the schema but is a ledger, so it blocks instead of being erased.
_BLOCKERS: tuple[tuple[str, str, str], ...] = (
    ("work orders", "work_orders", "assembly_id"),
    ("work order lines", "work_order_lines", "part_id"),
    ("customer order lines", "customer_order_lines", "part_id"),
    ("allocations", "allocations", "part_id"),
    ("despatch lines", "despatch_lines", "part_id"),
    ("purchase order lines", "purchase_order_lines", "part_id"),
    ("goods receipt lines", "goods_receipt_lines", "part_id"),
    ("stock movements", "stock_movements", "part_id"),
    ("assemblies using it", "bom_lines", "child_id"),
    ("devmgmt variants", "variants", "assembly_id"),
)


def group_ids(conn, part_id: int) -> list[int]:
    """Every version id in the part's group (a legacy un-grouped row is its own group)."""
    return [r["id"] for r in conn.execute(
        "SELECT id FROM parts WHERE COALESCE(version_group_id, id) = "
        "(SELECT COALESCE(version_group_id, id) FROM parts WHERE id = ?)", (part_id,))]


def is_retired(conn, part_id: int) -> bool:
    row = conn.execute("SELECT retired_at FROM parts WHERE id = ?", (part_id,)).fetchone()
    return bool(row and row["retired_at"])


def delete_blockers(conn, part_id: int) -> dict[str, int]:
    """Non-zero reference counts, across every version in the group, keyed by the label shown."""
    ids = group_ids(conn, part_id)
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    out: dict[str, int] = {}
    for label, table, column in _BLOCKERS:
        if not table_exists(conn, table):
            continue
        n = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {column} IN ({marks})", ids).fetchone()[0]
        if n:
            out[label] = n
    return out


def format_blockers(part_no: str, blockers: dict[str, int]) -> str:
    items = [f"used in {n} assemblies" if label == "assemblies using it" else f"{n} {label}"
             for label, n in blockers.items()]
    return f"Cannot delete {part_no}: {', '.join(items)}."


def _set_retired(db: Database, part_id: int, value_sql: str) -> None:
    with db.connect() as conn:
        ids = group_ids(conn, part_id)
        if not ids:
            raise ValueError("Part not found.")
        marks = ",".join("?" for _ in ids)
        conn.execute(
            f"UPDATE parts SET retired_at = {value_sql}, updated_at = datetime('now') "
            f"WHERE id IN ({marks})", ids)
        conn.commit()


def retire_part(db: Database, part_id: int) -> None:
    _set_retired(db, part_id, "datetime('now')")


def unretire_part(db: Database, part_id: int) -> None:
    _set_retired(db, part_id, "NULL")


def delete_part(db: Database, part_id: int) -> None:
    """Delete every version of a retired, unreferenced part. Raises ValueError otherwise."""
    with db.connect() as conn:
        row = conn.execute("SELECT part_no, retired_at FROM parts WHERE id = ?", (part_id,)).fetchone()
        if row is None:
            raise ValueError("Part not found.")
        if not row["retired_at"]:
            raise ValueError(f"Retire {row['part_no']} before deleting it.")
        blockers = delete_blockers(conn, part_id)
        if blockers:
            raise ValueError(format_blockers(row["part_no"], blockers))
        ids = group_ids(conn, part_id)
        marks = ",".join("?" for _ in ids)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"DELETE FROM parts WHERE id IN ({marks})", ids)
        conn.commit()
```

Check `Database.connect()` already turns `PRAGMA foreign_keys` on (grep `foreign_keys` in
`core/db.py`); if it does, drop the explicit PRAGMA line.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_lifecycle.py -q 2>&1 | tail -5`
Expected: all PASS.

- [ ] **Step 6: Full suite, then commit**

Run: `uv run pytest -q 2>&1 | tail -3`
Expected: 697 + new tests passed.

```bash
git add src/partpilot/web/features/catalog/migrations.py src/partpilot/web/features/catalog/lifecycle.py tests/test_lifecycle.py
git commit -m "Catalog: part lifecycle core — retired_at (v24), retire/unretire/delete for a version group"
```

---

### Task 2: Lists, summaries and detail flags

**Files:**
- Modify: `src/partpilot/web/features/catalog/repo.py` (`list_parts`, `summary`, `get_part`)
- Modify: `src/partpilot/web/features/catalog/router.py` (`parts_list` adds `retired: bool = False`)
- Modify: `src/partpilot/web/features/catalog/templates/parts_list.html`
- Modify: `src/partpilot/web/features/assemblies/repo.py` (`summary`, `list_assemblies`, `get_assembly` head + lines)
- Modify: `src/partpilot/web/features/assemblies/router.py` (`assemblies_list` adds `retired: bool = False`)
- Modify: `src/partpilot/web/features/assemblies/templates/assemblies_list.html`, `assembly_detail.html` (BOM row badge)
- Test: `tests/test_catalog.py`, `tests/test_assemblies.py`, `tests/test_web.py`

**Interfaces:**
- Consumes: `lifecycle.retire_part`.
- Produces: `catalog.repo.list_parts(..., include_retired: bool = False)`, `assemblies.repo.list_assemblies(db, search=None, include_retired=False)`, `get_part()["retired"]`, `get_assembly()["retired"]`, each BOM line `child_retired`.

- [ ] **Step 1: Write the failing repo tests**

Append to `tests/test_catalog.py`:

```python
from partpilot.web.features.catalog import lifecycle


def test_list_parts_hides_retired_unless_asked(db):
    a = repo.create_part(db, part={"part_no": "A-1"}, supplier_lines=[])
    b = repo.create_part(db, part={"part_no": "B-1"}, supplier_lines=[])
    lifecycle.retire_part(db, b)
    ids = {p["id"] for p in repo.list_parts(db)[0]}
    assert a in ids and b not in ids
    ids = {p["id"] for p in repo.list_parts(db, include_retired=True)[0]}
    assert b in ids
    assert repo.summary(db)["parts"] == 1
    assert repo.get_part(db, b)["retired"] is True
```

Append to `tests/test_assemblies.py`:

```python
from partpilot.web.features.catalog import lifecycle


def test_list_assemblies_hides_retired_unless_asked(db):
    a, s, c1, c2 = _setup(db)
    lifecycle.retire_part(db, s)
    ids = {r["id"] for r in repo.list_assemblies(db)}
    assert a in ids and s not in ids
    assert s in {r["id"] for r in repo.list_assemblies(db, include_retired=True)}
    assert repo.summary(db)["assemblies"] == 1
    detail = repo.get_assembly(db, a)
    assert detail["retired"] is False
    assert {ln["child_id"]: ln["child_retired"] for ln in detail["lines"]}[s] is True
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_catalog.py::test_list_parts_hides_retired_unless_asked tests/test_assemblies.py::test_list_assemblies_hides_retired_unless_asked -q 2>&1 | tail -5`
Expected: FAIL (`TypeError: unexpected keyword 'include_retired'`).

- [ ] **Step 3: Catalog repo changes**

In `list_parts`: add parameter `include_retired: bool = False`; in `where` add
`"AND (:retired = 1 OR p.retired_at IS NULL) "`; add `"retired": 1 if include_retired else 0` to
`params`; add `p.retired_at` to the SELECT list; after `d = dict(r)` add `d["retired"] = bool(d.get("retired_at"))`.

In `summary`: add `AND retired_at IS NULL` to all four counts.

In `get_part`: after `part["is_document"] = ...` add `part["retired"] = bool(part.get("retired_at"))`.

- [ ] **Step 4: Assemblies repo changes**

`summary`: add `AND retired_at IS NULL` / `AND p.retired_at IS NULL` to the three queries
(`lines` becomes `SELECT COUNT(*) FROM bom_lines b JOIN parts p ON p.id = b.parent_id WHERE p.retired_at IS NULL`).

`list_assemblies(db, search=None, include_retired=False)`: add `p.retired_at` to SELECT, add
`AND (:retired = 1 OR p.retired_at IS NULL)` to WHERE, pass `"retired": 1 if include_retired else 0`;
set `r["retired"] = bool(r["retired_at"])` on each returned dict.

`get_assembly`: add `c.retired_at AS child_retired_at` to the lines SELECT; in the per-line loop
`ln["child_retired"] = bool(ln.pop("child_retired_at"))`; after `assembly = dict(head)` add
`assembly["retired"] = bool(assembly.get("retired_at"))`.

- [ ] **Step 5: Run the repo tests**

Run: same command as Step 2. Expected: PASS.

- [ ] **Step 6: Write the failing web test**

Append to `tests/test_web.py`:

```python
def test_show_retired_toggle_on_parts_and_assemblies_lists(app):
    from partpilot.web.features.assemblies import repo as asmrepo
    from partpilot.web.features.catalog import lifecycle, repo as catrepo
    db = app.state.database
    pid = catrepo.create_part(db, part={"part_no": "OLD-COMP"}, supplier_lines=[])
    asm = asmrepo.create_assembly(db, {"part_no": "OLD-ASSY"})
    lifecycle.retire_part(db, pid)
    lifecycle.retire_part(db, asm)
    client = TestClient(app)
    _login(client, "buyer1", "pw")
    assert "OLD-COMP" not in client.get("/catalog").text
    page = client.get("/catalog?retired=1").text
    assert "OLD-COMP" in page and "Retired" in page
    assert "OLD-ASSY" not in client.get("/assemblies").text
    assert "OLD-ASSY" in client.get("/assemblies?retired=1").text
    assert "Retired" in client.get(f"/catalog/{pid}").text
```

Run: `uv run pytest tests/test_web.py::test_show_retired_toggle_on_parts_and_assemblies_lists -q 2>&1 | tail -4`
Expected: FAIL on the `?retired=1` assertion.

- [ ] **Step 7: Routers and templates**

`catalog/router.py` `parts_list`: add `retired: bool = False` param, pass
`include_retired=retired` to `repo.list_parts`, add `"retired": retired` to the context.

`parts_list.html`: after the "Normally stocked only" label add

```html
    <label style="display:flex; align-items:center; gap:.4rem; cursor:pointer; white-space:nowrap;
                  padding-bottom:.55rem;">
      <input name="retired" type="checkbox" value="1" style="width:auto;"
             {{ 'checked' if retired else '' }} onchange="this.form.submit()">
      Show retired
    </label>
```

Extend the `clear` condition to `{% if q or category or stocked or retired %}`; add
`&retired={{ '1' if retired else '' }}` to both pagination links; in the part_no cell append
`{% if p.retired %} <span class="badge muted">Retired</span>{% endif %}`.

`assemblies/router.py` `assemblies_list`: add `retired: bool = False`, pass
`include_retired=retired`, add `"retired": retired` to context. `assemblies_list.html`: same
checkbox after the search field (inside the form, before the Filter button), `clear` condition
`{% if q or retired %}`, badge after the part number link.

`part_detail.html` `<h1>`: add `{% if part.retired %}<span class="badge muted" title="Retired {{ part.retired_at }}">Retired</span>{% endif %}` after the ASSY badge.

`assembly_detail.html`: in the BOM row's part cell (after the `no BOM cost` badge) add
`{% if ln.child_retired %}<span class="badge muted" title="This part is retired — stock may run out, none will be bought">retired</span>{% endif %}`;
in the page's `<h1>` (find it above the cards) add the same **Retired** badge as the part page, keyed on `a.retired`.

- [ ] **Step 8: Run the web test, then the suite, then commit**

Run: `uv run pytest tests/test_web.py::test_show_retired_toggle_on_parts_and_assemblies_lists -q 2>&1 | tail -3` → PASS.
Run: `uv run pytest -q 2>&1 | tail -3` → all pass.

```bash
git add -A src/partpilot/web/features/catalog src/partpilot/web/features/assemblies tests/test_catalog.py tests/test_assemblies.py tests/test_web.py
git commit -m "Lists: hide retired parts and assemblies by default; Show retired toggle; Retired badges"
```

---

### Task 3: Pickers and server-side guards

**Files:**
- Modify: `src/partpilot/web/features/work_orders/repo.py` (`assemblies`, `create_work_order`, work-order lines query), `templates/work_order_detail.html`
- Modify: `src/partpilot/web/features/customer_orders/repo.py` (`parts_for_picker`, `add_line`)
- Modify: `src/partpilot/web/features/purchase_orders/repo.py` (`parts_for_picker`, `add_line`)
- Modify: `src/partpilot/web/features/assemblies/repo.py` (`parts_for_picker`, `add_bom_line`)
- Test: `tests/test_work_orders.py`, `tests/test_customer_orders.py`, `tests/test_purchase_orders.py`, `tests/test_assemblies.py`

**Interfaces:**
- Consumes: `lifecycle.retire_part`.

- [ ] **Step 1: Write the failing tests**

`tests/test_work_orders.py`:

```python
from partpilot.web.features.catalog import lifecycle


def test_retired_assembly_leaves_picker_and_cannot_be_built(db):
    a, b, sub, top = _setup(db)
    lifecycle.retire_part(db, top)
    assert top not in {r["id"] for r in repo.assemblies(db)}
    with pytest.raises(ValueError, match=r"TOP-1 is retired and cannot be built\."):
        repo.create_work_order(db, {"assembly_id": top, "qty": 1})


def test_retired_component_in_live_bom_still_builds_and_is_flagged(db):
    a, b, sub, top = _setup(db)
    lifecycle.retire_part(db, b)
    wo = repo.get_work_order(db, repo.create_work_order(db, {"assembly_id": top, "qty": 1}))
    by = {ln["part_no"]: ln for ln in wo["lines"]}
    assert by["COMP-B"]["retired"] is True and by["COMP-A"]["retired"] is False
```

`tests/test_customer_orders.py` (use the file's `_seed` helper to get a customer + order; read it
first and mirror how other tests create an order):

```python
from partpilot.web.features.catalog import lifecycle


def test_retired_part_leaves_order_picker_and_cannot_be_added(db):
    cust, part = _seed(db)[:2]  # adjust to _seed's actual return shape
    oid = repo.create_order(db, {"customer_id": cust})
    lifecycle.retire_part(db, part)
    assert part not in {p["id"] for p in repo.parts_for_picker(db)}
    with pytest.raises(ValueError, match=r"WIDGET-1 is retired and cannot be ordered\."):
        repo.add_line(db, oid, part, 1, None, None)
```

`tests/test_purchase_orders.py`:

```python
from partpilot.web.features.catalog import lifecycle


def test_retired_part_leaves_po_picker_and_cannot_be_added(db):
    p, asm, wo = _setup_shortage(db)
    sup = conrepo.create_contact(db, {"kind": "supplier", "name": "Acme Supplies"})
    po = repo.create_po(db, {"supplier_id": sup})
    lifecycle.retire_part(db, p)
    assert p not in {r["id"] for r in repo.parts_for_picker(db)}
    with pytest.raises(ValueError, match=r"R-1 is retired and cannot be ordered\."):
        repo.add_line(db, po, p, 5, 0.5)
```

`tests/test_assemblies.py`:

```python
def test_retired_part_leaves_bom_picker_and_cannot_be_added(db):
    a, s, c1, c2 = _setup(db)
    extra = _part(db, "C3", "PART", 3)
    lifecycle.retire_part(db, extra)
    assert extra not in {p["id"] for p in repo.parts_for_picker(db, a)}
    with pytest.raises(ValueError, match=r"C3 is retired and cannot be added to a BOM\."):
        repo.add_bom_line(db, a, extra, 1, None)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_work_orders.py tests/test_customer_orders.py tests/test_purchase_orders.py tests/test_assemblies.py -q -k retired 2>&1 | tail -8`
Expected: 5 FAIL (picker still lists the part / no ValueError).

- [ ] **Step 3: Implement**

Add `AND p.retired_at IS NULL` to the WHERE of: `work_orders.repo.assemblies` (alias `p` exists),
`customer_orders.repo.parts_for_picker`, `purchase_orders.repo.parts_for_picker` (rewrite as
`SELECT ... FROM parts p WHERE p.retired_at IS NULL ORDER BY part_no`), `assemblies.repo.parts_for_picker`.

Guards — replace the existence checks:

```python
# work_orders.repo.create_work_order
row = conn.execute("SELECT part_no, retired_at FROM parts WHERE id = ? AND kind = 'ASSY'",
                   (data["assembly_id"],)).fetchone()
if row is None:
    raise ValueError("Choose an assembly to build.")
if row["retired_at"]:
    raise ValueError(f"{row['part_no']} is retired and cannot be built.")

# customer_orders.repo.add_line (inside `if part_id is not None:`)
row = conn.execute("SELECT part_no, retired_at FROM parts WHERE id = ?", (part_id,)).fetchone()
if row is None:
    raise ValueError("Selected product was not found.")
if row["retired_at"]:
    raise ValueError(f"{row['part_no']} is retired and cannot be ordered.")

# purchase_orders.repo.add_line — same shape, message "...cannot be ordered."
# assemblies.repo.add_bom_line — same shape, message "...cannot be added to a BOM."
```

Work-order lines: add `p.retired_at` to the lines SELECT in `get_work_order`, then in the loop that
post-processes `lines` (or right after the list comprehension) set `ln["retired"] = bool(ln.get("retired_at"))`.
`work_order_detail.html` part cell: after the ASSY badge add
`{% if ln.retired %}<span class="badge muted" title="Retired part — none will be bought">retired</span>{% endif %}`.

- [ ] **Step 4: Run the tests, then the suite, then commit**

Run: Step 2 command → 5 PASS. Run: `uv run pytest -q 2>&1 | tail -3` → all pass.

```bash
git add -A src/partpilot/web/features tests
git commit -m "Pickers and guards: retired parts leave every picker; building, ordering and BOM-adding a retired part is refused"
```

---

### Task 4: Buying and resolving

**Files:**
- Modify: `src/partpilot/web/features/purchase_orders/repo.py` (`shortage_suggestions`)
- Modify: `src/partpilot/web/features/catalog/stock_index.py`
- Modify: `src/partpilot/web/features/catalog/repo.py` (`find_part_id_by_mpn`)
- Modify: `src/partpilot/web/features/catalog/import_parts.py` (`build_import_plan`, `apply_parts_plan`)
- Modify: `src/partpilot/web/features/catalog/woo_sync.py` (`_reconcile_one`)
- Test: `tests/test_purchase_orders.py`, `tests/test_versions.py`, `tests/test_catalog.py`, `tests/test_import_parts_engine.py`, `tests/test_woo_sync.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_purchase_orders.py`:

```python
def test_shortage_suggestions_skip_retired_parts(db):
    p, asm, wo = _setup_shortage(db)
    assert len(repo.shortage_suggestions(db)) == 1
    lifecycle.retire_part(db, p)
    assert repo.shortage_suggestions(db) == []
```

`tests/test_versions.py` (next to `test_stock_index_excludes_non_released_versions`):

```python
def test_stock_index_excludes_retired_parts(db):
    from partpilot.web.features.catalog import lifecycle
    pid = _mk_part(db, part_no="GRM-9", rev="A", qty=100.0)
    lifecycle.retire_part(db, pid)
    assert pid not in {it.item_id for it in stock_index.build_stock_index(db).items}
```

`tests/test_catalog.py`:

```python
def test_find_part_id_by_mpn_ignores_retired_unless_asked(db):
    pid = repo.create_part(db, part={"part_no": "MPN-9"}, supplier_lines=[])
    lifecycle.retire_part(db, pid)
    assert repo.find_part_id_by_mpn(db, "MPN-9") is None
    assert repo.find_part_id_by_mpn(db, "MPN-9", include_retired=True) == pid
    assert repo.find_part_by_part_no(db, "MPN-9")["id"] == pid   # the number stays taken
```

`tests/test_import_parts_engine.py`:

```python
def test_apply_parts_plan_neither_links_nor_creates_a_retired_match(db):
    from partpilot.web.features.catalog import lifecycle
    old = catrepo.create_part(db, part={"part_no": "RET-1"}, supplier_lines=[])
    lifecycle.retire_part(db, old)
    plan = [{"status": "new", "part_no": "RET-1", "mfr_pno": "RET-1", "qty": 1, "refdes": "C1"}]
    res = import_parts.apply_parts_plan(db, plan, accepted={0})
    assert res == {**res, "created": 0, "linked": 0, "skipped": 1}
    assert res["part_ids"] == [None]
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM parts WHERE part_no = 'RET-1'").fetchone()[0] == 1
```

Also a `build_import_plan` test: look at how `tests/test_import_parts_engine.py` or
`tests/test_shelves_resolver.py` constructs a `ResolvedRun` with an `IN_STOCK`/`RESOLVED` line and
write `test_build_import_plan_flags_retired_match` asserting the item has `status == "new"` (not
`in_inventory`), `part_id is None` and `note == "matches retired part RET-1"`.

`tests/test_woo_sync.py`:

```python
def test_retired_part_is_skipped_not_duplicated(db):
    from partpilot.web.features.catalog import lifecycle
    pid = _make_part(db, "99-100", 5)
    lifecycle.retire_part(db, pid)
    report = woo_sync.sync_from_woo(db, [_p("99-100", qty=10)])
    assert report.skipped == 1
    assert any(n["detail"] == "part is retired" for n in report.notes)   # adjust to SyncReport's note shape
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM parts WHERE part_no = '99-100'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM stock_movements WHERE part_id = ?", (pid,)).fetchone()[0] == 1  # opening only
```

Read `SyncReport` (`woo_sync.py` top) for the real attribute holding notes before asserting.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_purchase_orders.py tests/test_versions.py tests/test_catalog.py tests/test_import_parts_engine.py tests/test_woo_sync.py -q -k "retired" 2>&1 | tail -8`
Expected: all new tests FAIL.

- [ ] **Step 3: Implement**

`shortage_suggestions`: change the part lookup to
`SELECT part_no, value, unlimited_stock, retired_at FROM parts WHERE id = ?` and the skip to
`if part is None or part["unlimited_stock"] or part["retired_at"]: continue  # retired: run out, never re-buy`.

`stock_index.build_stock_index`: append `f"AND p.retired_at IS NULL"` after the released filter.

`catalog.repo.find_part_id_by_mpn(db, mpn, *, include_retired: bool = False)`: add
`AND (? = 1 OR retired_at IS NULL)` to the WHERE with the extra bind `1 if include_retired else 0`
(bind order: mpn, mpn, flag). `find_part_by_part_no` is unchanged.

`import_parts.build_import_plan`: after `existing_id = catalog_repo.find_part_id_by_mpn(db, mpn)`:

```python
        retired_id = None if existing_id is not None else catalog_repo.find_part_id_by_mpn(
            db, mpn, include_retired=True)
        if retired_id is not None:
            old = catalog_repo.get_part(db, retired_id)
            item["note"] = f"matches retired part {old['part_no']}"
            item["retired_match"] = True
```

(the line then falls through to the NEW/UNRESOLVED branches as today.)

`import_parts.apply_parts_plan`, inside the accepted NEW/UNRESOLVED branch, before the lookup:

```python
            if catalog_repo.find_part_id_by_mpn(db, item.get("part_no")) is None and \
               catalog_repo.find_part_id_by_mpn(db, item.get("part_no"), include_retired=True) is not None:
                skipped += 1                   # only match is retired: never link, never duplicate
                part_ids.append(None)
                continue
```

`woo_sync._reconcile_one`, after `existing = repo.find_part_by_part_no(db, sku)` / `if existing is None` block:

```python
    if existing.get("retired_at"):
        report.skipped += 1
        report._note(sku, "skipped", "part is retired")
        return
```

- [ ] **Step 4: Run the tests, the suite, commit**

Run: Step 2 command → PASS. Run: `uv run pytest -q 2>&1 | tail -3` → all pass.

```bash
git add -A src/partpilot/web/features/catalog src/partpilot/web/features/purchase_orders tests
git commit -m "Buying and resolving: retired parts are never suggested, never matched by the stock check, importer and webshop sync"
```

---

### Task 5: Routes and buttons

**Files:**
- Modify: `src/partpilot/web/features/catalog/router.py` (add `_render_part_detail`, three POST routes)
- Modify: `src/partpilot/web/features/catalog/templates/part_detail.html`
- Modify: `src/partpilot/web/features/assemblies/router.py` (three POST routes)
- Modify: `src/partpilot/web/features/assemblies/templates/assembly_detail.html`
- Modify: `docs/user-manual.md` (same commit — new buttons)
- Test: `tests/test_web.py`

- [ ] **Step 1: Write the failing web tests**

```python
def test_part_lifecycle_routes_and_roles(app):
    from partpilot.web.features.catalog import repo as catrepo
    db = app.state.database
    app.state.store.create_user("boss", "pw", role="admin")
    pid = catrepo.create_part(db, part={"part_no": "LC-1"}, supplier_lines=[])
    buyer, admin = TestClient(app), TestClient(app)
    _login(buyer, "buyer1", "pw"); _login(admin, "boss", "pw")

    page = buyer.get(f"/catalog/{pid}").text
    assert "Retire" in page and "Delete" not in page
    # delete before retire → 400 with the refusal in .err
    r = admin.post(f"/catalog/{pid}/delete", follow_redirects=False)
    assert r.status_code == 400 and "Retire LC-1 before deleting it." in r.text
    # purchasing retires
    assert buyer.post(f"/catalog/{pid}/retire", follow_redirects=False).status_code == 303
    assert catrepo.get_part(db, pid)["retired"] is True
    page = admin.get(f"/catalog/{pid}").text
    assert "Unretire" in page and "Delete" in page
    assert "Delete" not in buyer.get(f"/catalog/{pid}").text        # admin-only button
    # purchasing cannot delete
    assert buyer.post(f"/catalog/{pid}/delete", follow_redirects=False).status_code == 403
    # admin deletes → list
    r = admin.post(f"/catalog/{pid}/delete", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/catalog"
    assert catrepo.get_part(db, pid) is None


def test_assembly_lifecycle_routes(app):
    from partpilot.web.features.assemblies import repo as asmrepo
    from partpilot.web.features.catalog import repo as catrepo
    db = app.state.database
    app.state.store.create_user("boss", "pw", role="admin")
    asm = asmrepo.create_assembly(db, {"part_no": "LC-ASSY"})
    comp = catrepo.create_part(db, part={"part_no": "LC-C"}, supplier_lines=[])
    asmrepo.add_bom_line(db, asm, comp, 1, None)
    admin = TestClient(app); _login(admin, "boss", "pw")
    assert admin.post(f"/assemblies/{asm}/retire", follow_redirects=False).status_code == 303
    assert "Retired" in admin.get(f"/assemblies/{asm}").text
    assert admin.post(f"/assemblies/{asm}/unretire", follow_redirects=False).status_code == 303
    assert asmrepo.get_assembly(db, asm)["retired"] is False
    admin.post(f"/assemblies/{asm}/retire")
    r = admin.post(f"/assemblies/{asm}/delete", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/assemblies"
    assert asmrepo.get_assembly(db, asm) is None
    assert catrepo.get_part(db, comp) is not None
```

Run: `uv run pytest tests/test_web.py -q -k lifecycle 2>&1 | tail -4` → FAIL (404 on the POST routes).

- [ ] **Step 2: Catalog router**

Imports: `from . import lifecycle, repo, stock, versions`. Add `PART_DELETE_ROLES = frozenset({"admin"})`.

Refactor `part_detail` into a helper and route:

```python
def _render_part_detail(request: Request, part_id: int, user, *, error: str | None = None,
                        status: int = 200, shelf: int | None = None):
    db = request.app.state.database
    templates = request.app.state.templates
    part = repo.get_part(db, part_id)
    if part is None:
        return templates.TemplateResponse(
            request, "error.html", {"message": "Part not found."}, status_code=404)
    group_id = part.get("version_group_id") or part_id
    return templates.TemplateResponse(
        request, "part_detail.html",
        {"part": part, "can_edit": user.role in CATALOG_WRITE_ROLES,
         "can_delete": user.role in PART_DELETE_ROLES,
         "can_move": user.role in STOCK_MOVE_ROLES,
         "movements": stock.movements_for_part(db, part_id),
         "locations": repo.locations(db), "shelf_selected": shelf,
         "versions": versions.list_versions(db, group_id), "version_base": "/catalog",
         "error": error},
        status_code=status)


@router.get("/{part_id}", response_class=HTMLResponse)
def part_detail(request: Request, part_id: int, shelf: int | None = None):
    user = require_user(request)
    return _render_part_detail(request, part_id, user, shelf=shelf)


@router.post("/{part_id}/retire")
def retire(request: Request, part_id: int):
    user = require_role(request, CATALOG_WRITE_ROLES)
    try:
        lifecycle.retire_part(request.app.state.database, part_id)
    except ValueError as exc:
        return _render_part_detail(request, part_id, user, error=str(exc), status=400)
    return RedirectResponse(f"/catalog/{part_id}", status_code=303)


@router.post("/{part_id}/unretire")
def unretire(request: Request, part_id: int):
    user = require_role(request, CATALOG_WRITE_ROLES)
    try:
        lifecycle.unretire_part(request.app.state.database, part_id)
    except ValueError as exc:
        return _render_part_detail(request, part_id, user, error=str(exc), status=400)
    return RedirectResponse(f"/catalog/{part_id}", status_code=303)


@router.post("/{part_id}/delete")
def delete(request: Request, part_id: int):
    user = require_role(request, PART_DELETE_ROLES)
    try:
        lifecycle.delete_part(request.app.state.database, part_id)
    except ValueError as exc:
        return _render_part_detail(request, part_id, user, error=str(exc), status=400)
    return RedirectResponse("/catalog", status_code=303)
```

Place the three POST routes **before** `@router.get("/{part_id}")`? Not required (different
methods), but keep them right after it for readability. `shelves/...` routes already precede
`/{part_id}` — do not reorder them.

`part_detail.html`: replace the single Edit link block with an action bar:

```html
    {% if can_edit %}
    <div class="act-bar" style="margin-top:0;">
      <a class="act-btn" href="/catalog/{{ part.id }}/edit">Edit</a>
      {% if not part.retired %}
      <form method="post" action="/catalog/{{ part.id }}/retire"
            onsubmit="return confirm('Retire {{ part.part_no }}? It leaves the lists and pickers and will not be bought again. Existing stock can still be used.');">
        <button type="submit" class="act-btn">Retire</button>
      </form>
      {% else %}
      <form method="post" action="/catalog/{{ part.id }}/unretire">
        <button type="submit" class="act-btn">Unretire</button>
      </form>
      {% if can_delete %}
      <form method="post" action="/catalog/{{ part.id }}/delete"
            onsubmit="return confirm('Delete {{ part.part_no }} permanently? Only possible when nothing references it.');">
        <button type="submit" class="act-btn danger">Delete</button>
      </form>
      {% endif %}
      {% endif %}
    </div>
    {% endif %}
```

Check `docs/design-rules.md` §2 for the danger button class name (`danger` / `.act-btn.bad`); use
the documented one. Add `{% if error %}<div class="err" style="margin-top:.8rem;">{{ error }}</div>{% endif %}`
right after the header `<div>` closes (before the `.cards`). The part page has no `.act-bar` CSS of
its own — the assembly page defines it inline; check `base.html`'s stylesheet for `.act-bar`; if
absent there, copy the four `.act-bar`/`.act-btn` rules from `assembly_detail.html` into a
`<style>` block at the top of `part_detail.html`.

- [ ] **Step 3: Assemblies router and template**

Imports: `from ..catalog import devmgmt_outbox, devmgmt_repo, lifecycle, versions as catalog_versions`;
add `ASSEMBLY_DELETE_ROLES = frozenset({"admin"})`. Add `"can_delete": user.role in ASSEMBLY_DELETE_ROLES`
to `_render_detail`'s context. Routes, after `convert_to_component`:

```python
@router.post("/{part_id}/retire")
def retire(request: Request, part_id: int):
    user = require_role(request, ASSEMBLY_WRITE_ROLES)
    try:
        lifecycle.retire_part(request.app.state.database, part_id)
    except ValueError as exc:
        return _render_detail(request, part_id, user, error=str(exc), status=400)
    return RedirectResponse(f"/assemblies/{part_id}", status_code=303)


@router.post("/{part_id}/unretire")
def unretire(request: Request, part_id: int):
    user = require_role(request, ASSEMBLY_WRITE_ROLES)
    try:
        lifecycle.unretire_part(request.app.state.database, part_id)
    except ValueError as exc:
        return _render_detail(request, part_id, user, error=str(exc), status=400)
    return RedirectResponse(f"/assemblies/{part_id}", status_code=303)


@router.post("/{part_id}/delete")
def delete(request: Request, part_id: int):
    user = require_role(request, ASSEMBLY_DELETE_ROLES)
    try:
        lifecycle.delete_part(request.app.state.database, part_id)
    except ValueError as exc:
        return _render_detail(request, part_id, user, error=str(exc), status=400)
    return RedirectResponse("/assemblies", status_code=303)
```

`assembly_detail.html` action bar, after the Convert button inside `{% if can_edit %}`:

```html
    {% if not a.retired %}
    <button type="submit" class="act-btn" form="retireForm">Retire</button>
    {% else %}
    <button type="submit" class="act-btn" form="unretireForm">Unretire</button>
    {% if can_delete %}<button type="submit" class="act-btn danger" form="deleteForm">Delete</button>{% endif %}
    {% endif %}
```

and next to `convertForm`:

```html
  <form id="retireForm" method="post" action="/assemblies/{{ a.id }}/retire"
        onsubmit="return confirm('Retire {{ a.part_no }}? No new work orders can be raised for it and it leaves the lists and pickers. Existing stock can still be despatched.');"></form>
  <form id="unretireForm" method="post" action="/assemblies/{{ a.id }}/unretire"></form>
  <form id="deleteForm" method="post" action="/assemblies/{{ a.id }}/delete"
        onsubmit="return confirm('Delete {{ a.part_no }} permanently? Only possible when no order, work order, stock movement or parent BOM references it.');"></form>
```

- [ ] **Step 4: Manual edits (same commit)**

In `docs/user-manual.md` under *Parts*, after "### Cleaning up distributor order codes", add:

```markdown
### Retiring and deleting a part

A part is never deleted straight away. First **"Retire"** it (admin or purchasing): the part leaves
the Parts list and every picker, is never suggested for purchase, and the BOM resolver no longer
counts its stock. Everything that already references it keeps working: existing stock can still be
issued or despatched, old orders and work orders still show it, and an assembly whose BOM lists it
still builds and consumes it (the BOM line shows a **retired** badge). Tick **"Show retired"** on
the Parts list to see retired parts; a retired part carries a **Retired** badge and an
**"Unretire"** button.

Only an admin can **"Delete"** a part, and only once it is retired. Deleting removes every version
of the part together with its supplier offers, price tiers and stock rows. It is refused while
anything historical still points at the part: work orders or their lines, customer order lines,
despatch lines, purchase order or goods receipt lines, stock movements, another assembly's BOM, or
a devmgmt variant. The refusal lists what blocks it, for example *"Cannot delete R-1: 3 work orders,
12 stock movements, used in 2 assemblies."* In practice a part that has ever been built, bought or
sold can only be retired; delete is for parts created by mistake.
```

Under *Assemblies*, after "### Versions of an assembly", add:

```markdown
### Retiring and deleting an assembly

Assemblies follow the part rules above. **"Retire"** (admin or purchasing) removes the assembly from
the Assemblies list, the **New Work Order** picker and the customer-order product picker, and
refuses new work orders for it; finished boards on the shelf can still be despatched. Tick
**"Show retired"** on the Assemblies list to find it again, and **"Unretire"** brings it back.
**"Delete"** (admin only, after retiring) removes the assembly and its own BOM lines; it is refused
while any work order, order line, stock movement, parent BOM or devmgmt variant references it.
```

In the *Assemblies → The assembly page* "Actions" list add `- **"Retire"** / **"Unretire"** /
**"Delete"**: see *Retiring and deleting an assembly*.` In *Work orders → Creating a stand-alone
work order* change "(only assemblies with a BOM are offered)" to "(only active assemblies with a BOM
are offered; a retired assembly cannot be built)". In *Buying → Purchase suggestions from shortages*
add a bullet: `- A **retired** part is never suggested, even when a work order is short of it: retired
means run out, don't re-buy.`

- [ ] **Step 5: Run the tests, the suite, commit**

Run: `uv run pytest tests/test_web.py -q -k lifecycle 2>&1 | tail -3` → PASS.
Run: `uv run pytest -q 2>&1 | tail -3` → all pass.

```bash
git add -A src/partpilot/web/features/catalog src/partpilot/web/features/assemblies tests/test_web.py docs/user-manual.md
git commit -m "Part and assembly pages: Retire / Unretire / Delete (admin) with refusals in .err; manual chapters"
```

---

### Task 6: Docs

**Files:**
- Modify: `docs/design-rules.md` (change log), `README.md` (Parts + Assemblies sections), `CLAUDE.md` (one bullet under the web-app architecture notes)

- [ ] **Step 1: design-rules change log** — append:

```markdown
- **2026-10-16** — **Part lifecycle (retire → delete).** Part and assembly pages carry **Retire**
  (active) or **Unretire** + admin-only **Delete** (retired) as real buttons in the `.act-bar`,
  each destructive one behind `confirm()`; refusals render in `.err`. A retired part shows a
  `badge muted` **Retired** in its `<h1>` and as a `retired` pill on BOM and work-order lines that
  still reference it. Lists hide retired rows behind a **Show retired** checkbox that re-submits the
  filter form (same pattern as "Normally stocked only").
```

- [ ] **Step 2: README** — in `### Parts (`catalog`)` add one line: "Parts can be **retired**
(hidden, never bought or built again, stock runs out) and, once retired and unreferenced, **deleted**
by an admin." In `### Assemblies` add: "Retire / Unretire / Delete follow the part lifecycle."

- [ ] **Step 3: CLAUDE.md** — under "The web app" bullets add: "**Part lifecycle:** `parts.retired_at`
(catalog v24) + `catalog/lifecycle.py`. Every picker / suggestion / stock-index query filters
`retired_at IS NULL`; write paths that take a part id refuse retired parts; delete is admin-only and
blocked by any referencing row (`lifecycle.delete_blockers`). New features that list parts for
selection must add the same filter."

- [ ] **Step 4: Commit**

```bash
git add docs/design-rules.md README.md CLAUDE.md
git commit -m "Docs: part lifecycle (retire, admin-only delete) in README, CLAUDE.md and design rules"
```
