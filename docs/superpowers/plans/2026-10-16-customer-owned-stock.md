# Customer-owned stock (shelves) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make stock ownership a property of *where stock sits* (a "shelf" = `stock_locations` row with kind `ours`/`customer` and an owner), so parts bought for a customer never mix with ours, while the catalog keeps one part per MPN.

**Architecture:** Extend `stock_locations` with kind/owner/order/retired; make the stock core strict per shelf and roll `parts.total_qty/total_alloc` up over *ours* shelves only (new `parts.customer_qty` for customer shelves). Give POs a receive-into shelf, make WOs issue strictly from their shelf, make allocations/despatch shelf-aware, build the resolver's stock index from one chosen shelf, and replace the two stock checkboxes on the upload screens with one shelf selector. Shelf pages live in the catalog feature; contact and customer-order pages get create buttons.

**Tech Stack:** FastAPI + Jinja (hot-reload templates), raw sqlite3 via `Database`, pytest + `TestClient`, `uv run`.

**Spec:** `docs/superpowers/specs/2026-10-16-customer-owned-stock-design.md`

## Global Constraints

- Migrations are additive; never edit a shipped one. New versions: catalog **22**, purchase_orders **5**, customer_orders **7**.
- `owner_contact_id` / `customer_order_id` on `stock_locations` are plain INTEGER (no FK — catalog migrates before contacts/customer_orders).
- `parts.total_qty` / `total_alloc` sum **ours** shelves only; `parts.customer_qty` sums customer shelves.
- `post_movement` requires `location_id`; **no fallback** to another shelf's row.
- UI follows `docs/design-rules.md`: `.panel/.note/.cards/.badge {ok|warn|bad|stock|muted}`, real buttons in rows, inline button pairs = `ghost` + `quiet`, no inline hex.
- Roles: shelf list/detail any user; create `PURCHASE_ROLES`; rename/retire `{"admin"}`.
- Commit after every task; banner 🟥 for behaviour changes at delivery (spec "Behaviour change callouts").
- `uv run pytest` must be green at the end of every task.

## Review Focus

1. A part edit form (`update_part`) that changes the stock row's location must never re-home a **customer** shelf row into Main — Task 4 pins it (edit targets the default own shelf only).
2. Receiving a PO whose `location_id` is NULL (legacy row) must land on the default own shelf, not crash — Task 5 test `test_receive_legacy_po_without_shelf_lands_on_main`.
3. A WO created with a NULL/omitted shelf must behave as Main (issue/finish/availability) — Task 6 test `test_work_order_without_shelf_uses_default`.
4. `allocate_order` for an order whose customer has **no** shelf must still allocate from own shelves — Task 7 test `test_allocate_without_customer_shelf_uses_ours`.
5. Upload pages posting `stock_shelf` for a **retired** shelf must 400, not silently check Main — Task 8 test `test_upload_rejects_retired_shelf`.

---

### Task 1: Stock core — strict per-shelf movements, ours-only rollup, migration 22

**Files:**
- Modify: `src/partpilot/web/features/catalog/migrations.py` (append v22)
- Modify: `src/partpilot/web/features/catalog/stock.py`
- Modify: `src/partpilot/web/features/catalog/repo.py` (`_location_id` → `default_shelf`, opening/edit stock paths)
- Modify: `src/partpilot/web/features/catalog/router.py:410-440` (`move_stock` passes shelf; adjust targets shelf row)
- Modify: `src/partpilot/web/features/customer_orders/repo.py:407-418`, `src/partpilot/web/features/despatch/repo.py:14-23` (delete `_recompute_part_alloc`, call `stock.recompute_alloc`)
- Test: `tests/test_shelves_core.py`

**Interfaces:**
- Produces: `stock.post_movement(conn, part_id, *, delta, mtype, location_id, reference=None, note=None, user=None) -> float` (shelf on-hand after); `stock.adjust_stock(db, part_id, *, delta, mtype, location_id, reference=None, note=None, user=None)`; `stock.default_shelf(conn) -> int`; `stock.recompute_alloc(conn, part_id) -> None`; `stock.free_on_shelf(conn, part_id, location_id) -> float`; `stock.shelf_on_hand(conn, part_id, location_id) -> float`.
- Produces: columns `stock_locations.kind/owner_contact_id/customer_order_id/created_at/retired_at`, `parts.customer_qty`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_shelves_core.py
import pytest

from partpilot.web.core import FeatureRegistry
from partpilot.web.core.db import Database
from partpilot.web.features.catalog import feature as catalog_feature
from partpilot.web.features.catalog import repo as catrepo
from partpilot.web.features.catalog import stock


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "shelves.db")
    reg = FeatureRegistry()
    reg.register(catalog_feature)
    database.apply_migrations(reg)
    return database


def _part(db, part_no="R-1", qty=0):
    return catrepo.create_part(db, part={"part_no": part_no}, supplier_lines=[],
                               opening={"qty": qty} if qty else None)


def _shelf(db, name, kind="ours"):
    with db.connect() as conn:
        sid = conn.execute("INSERT INTO stock_locations (name, kind) VALUES (?, ?)",
                           (name, kind)).lastrowid
        conn.commit()
    return sid


def test_migration_adds_shelf_columns_and_main_is_ours(db):
    with db.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(stock_locations)")}
        assert {"kind", "owner_contact_id", "customer_order_id", "created_at", "retired_at"} <= cols
        pcols = {r["name"] for r in conn.execute("PRAGMA table_info(parts)")}
        assert "customer_qty" in pcols
        main = stock.default_shelf(conn)
        assert conn.execute("SELECT kind FROM stock_locations WHERE id = ?", (main,)).fetchone()["kind"] == "ours"


def test_movement_on_new_shelf_creates_row_and_leaves_other_shelf_alone(db):
    pid = _part(db, qty=100)                    # 100 on Main (OPENING)
    cust = _shelf(db, "Acme", kind="customer")
    with db.connect() as conn:
        after = stock.post_movement(conn, pid, delta=-40, mtype=stock.ISSUE, location_id=cust)
        conn.commit()
        rows = {r["location_id"]: r["on_hand"] for r in conn.execute(
            "SELECT location_id, on_hand FROM part_stock WHERE part_id = ?", (pid,))}
    assert after == -40                          # the shelf's own balance, allowed negative
    assert rows[cust] == -40
    assert rows[stock_main(db)] == 100          # Main untouched: no fallback


def stock_main(db):
    with db.connect() as conn:
        return stock.default_shelf(conn)


def test_rollup_splits_ours_and_customer_totals(db):
    pid = _part(db, qty=100)
    cust = _shelf(db, "Acme", kind="customer")
    with db.connect() as conn:
        stock.post_movement(conn, pid, delta=25, mtype=stock.RECEIVE, location_id=cust)
        conn.commit()
        p = conn.execute("SELECT total_qty, customer_qty FROM parts WHERE id = ?", (pid,)).fetchone()
    assert p["total_qty"] == 100 and p["customer_qty"] == 25


def test_movement_without_shelf_is_refused(db):
    pid = _part(db)
    with db.connect() as conn, pytest.raises(ValueError):
        stock.post_movement(conn, pid, delta=1, mtype=stock.ADJUST, location_id=None)


def test_ledger_qty_after_is_shelf_balance(db):
    pid = _part(db, qty=10)
    cust = _shelf(db, "Acme", kind="customer")
    with db.connect() as conn:
        stock.post_movement(conn, pid, delta=5, mtype=stock.RECEIVE, location_id=cust)
        conn.commit()
        m = conn.execute("SELECT qty_after FROM stock_movements WHERE part_id = ? ORDER BY id DESC LIMIT 1",
                         (pid,)).fetchone()
    assert m["qty_after"] == 5


def test_free_on_shelf_and_recompute_alloc(db):
    pid = _part(db, qty=10)
    cust = _shelf(db, "Acme", kind="customer")
    with db.connect() as conn:
        main = stock.default_shelf(conn)
        stock.post_movement(conn, pid, delta=7, mtype=stock.RECEIVE, location_id=cust)
        # Fake allocation rows (table lives in customer_orders; create a stand-in for this unit test).
        conn.execute("CREATE TABLE IF NOT EXISTS allocations (id INTEGER PRIMARY KEY, "
                     "customer_order_line_id INTEGER, part_id INTEGER, qty REAL, location_id INTEGER)")
        conn.execute("INSERT INTO allocations (customer_order_line_id, part_id, qty, location_id) VALUES (1, ?, 3, ?)",
                     (pid, cust))
        conn.execute("INSERT INTO allocations (customer_order_line_id, part_id, qty, location_id) VALUES (1, ?, 4, ?)",
                     (pid, main))
        stock.recompute_alloc(conn, pid)
        conn.commit()
        assert stock.free_on_shelf(conn, pid, cust) == 4      # 7 - 3
        assert stock.free_on_shelf(conn, pid, main) == 6      # 10 - 4
        assert conn.execute("SELECT total_alloc FROM parts WHERE id = ?", (pid,)).fetchone()[0] == 4  # ours only
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_shelves_core.py -q`
Expected: FAIL — `AttributeError: module ... has no attribute 'default_shelf'` / missing columns.

- [ ] **Step 3: Add migration 22**

Append to the list in `src/partpilot/web/features/catalog/migrations.py`:

```python
    Migration(
        version=22,
        name="stock shelves (customer-owned stock)",
        sql="""
        -- A shelf is a stock location with an owner. kind 'ours' = our stock; 'customer' = parts the
        -- customer paid for up front and that only their builds may use. owner/order ids are soft
        -- joins (catalog migrates before contacts / customer_orders).
        ALTER TABLE stock_locations ADD COLUMN kind TEXT NOT NULL DEFAULT 'ours';
        ALTER TABLE stock_locations ADD COLUMN owner_contact_id INTEGER;
        ALTER TABLE stock_locations ADD COLUMN customer_order_id INTEGER;
        ALTER TABLE stock_locations ADD COLUMN created_at TEXT NOT NULL DEFAULT (datetime('now'));
        ALTER TABLE stock_locations ADD COLUMN retired_at TEXT;
        CREATE UNIQUE INDEX IF NOT EXISTS ux_stockloc_name ON stock_locations(name);
        -- parts.total_qty / total_alloc now mean OUR stock only; customer shelves roll up here.
        ALTER TABLE parts ADD COLUMN customer_qty REAL NOT NULL DEFAULT 0;
        -- Every stock row and ledger row belongs to a shelf (safety net; live DB has no null rows).
        INSERT INTO stock_locations (name) SELECT 'Main' WHERE NOT EXISTS (SELECT 1 FROM stock_locations);
        UPDATE part_stock SET location_id = (SELECT MIN(id) FROM stock_locations) WHERE location_id IS NULL;
        UPDATE stock_movements SET location_id = (SELECT MIN(id) FROM stock_locations) WHERE location_id IS NULL;
        """,
    ),
```

- [ ] **Step 4: Rewrite `stock.py`**

```python
"""The one place stock quantities change.

`post_movement` applies a signed delta to a part's on-hand ON ONE SHELF, rolls the part's totals up
(`total_qty`/`total_alloc` = our shelves only, `customer_qty` = customer shelves) and writes a
`stock_movements` ledger row — atomically, inside the caller's transaction. It never touches another
shelf's row: a customer build that lacks a part goes negative on its own shelf instead of silently
consuming Main.
"""

from __future__ import annotations

from ...core.db import Database

RECEIVE = "RECEIVE"   # goods in (+)
ISSUE = "ISSUE"       # consumed by a build / shipped (-)
WOOSALE = "WOOSALE"   # sold via the WooCommerce webshop (-)
BUILD = "BUILD"       # finished assembly into stock (+)
ADJUST = "ADJUST"     # manual correction (+/-)
OPENING = "OPENING"   # opening balance (+)


def default_shelf(conn) -> int:
    """Our default shelf: the lowest-id active 'ours' shelf ("Main"), created if none exists."""
    row = conn.execute(
        "SELECT id FROM stock_locations WHERE kind = 'ours' AND retired_at IS NULL ORDER BY id LIMIT 1"
    ).fetchone()
    if row:
        return row["id"]
    return conn.execute("INSERT INTO stock_locations (name, kind) VALUES ('Main', 'ours')").lastrowid


def _rollup(conn, part_id: int) -> None:
    conn.execute(
        """UPDATE parts SET
             total_qty = (SELECT COALESCE(SUM(pk.on_hand), 0) FROM part_stock pk
                          JOIN stock_locations l ON l.id = pk.location_id
                          WHERE pk.part_id = ? AND l.kind = 'ours'),
             customer_qty = (SELECT COALESCE(SUM(pk.on_hand), 0) FROM part_stock pk
                             JOIN stock_locations l ON l.id = pk.location_id
                             WHERE pk.part_id = ? AND l.kind = 'customer'),
             updated_at = datetime('now')
           WHERE id = ?""",
        (part_id, part_id, part_id),
    )


def post_movement(conn, part_id: int, *, delta: float, mtype: str, location_id: int | None,
                  reference: str | None = None, note: str | None = None,
                  user: str | None = None) -> float:
    """Apply ``delta`` on shelf ``location_id`` and record it. Returns the shelf's on-hand after.
    Negative results are allowed (callers that must not go negative check availability first)."""
    if not location_id:
        raise ValueError("A stock movement needs a shelf.")
    row = conn.execute(
        "SELECT id, on_hand FROM part_stock WHERE part_id = ? AND location_id = ? ORDER BY id LIMIT 1",
        (part_id, location_id),
    ).fetchone()
    if row is None:
        stock_id = conn.execute(
            "INSERT INTO part_stock (part_id, location_id, on_hand) VALUES (?, ?, 0)",
            (part_id, location_id)).lastrowid
        old = 0.0
    else:
        stock_id, old = row["id"], row["on_hand"] or 0.0
    after = old + delta
    conn.execute("UPDATE part_stock SET on_hand = ? WHERE id = ?", (after, stock_id))
    _rollup(conn, part_id)
    conn.execute(
        "INSERT INTO stock_movements (part_id, location_id, mtype, qty_delta, qty_after, "
        "reference, note, user) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (part_id, location_id, mtype, delta, after, reference, note, user),
    )
    return after


def adjust_stock(db: Database, part_id: int, *, delta: float, mtype: str, location_id: int | None,
                 reference: str | None = None, note: str | None = None,
                 user: str | None = None) -> float:
    """Standalone single movement (opens + commits its own transaction)."""
    with db.connect() as conn:
        after = post_movement(conn, part_id, delta=delta, mtype=mtype, reference=reference,
                              note=note, user=user, location_id=location_id)
        conn.commit()
    return after


def shelf_on_hand(conn, part_id: int, location_id: int) -> float:
    row = conn.execute("SELECT on_hand FROM part_stock WHERE part_id = ? AND location_id = ?",
                       (part_id, location_id)).fetchone()
    return float(row["on_hand"] or 0) if row else 0.0


def free_on_shelf(conn, part_id: int, location_id: int) -> float:
    row = conn.execute("SELECT on_hand, allocated FROM part_stock WHERE part_id = ? AND location_id = ?",
                       (part_id, location_id)).fetchone()
    return float((row["on_hand"] or 0) - (row["allocated"] or 0)) if row else 0.0


def recompute_alloc(conn, part_id: int) -> None:
    """Mirror `allocations` onto part_stock.allocated per shelf and parts.total_alloc (ours only)."""
    conn.execute("UPDATE part_stock SET allocated = 0 WHERE part_id = ?", (part_id,))
    for r in conn.execute(
        "SELECT location_id, COALESCE(SUM(qty), 0) AS qty FROM allocations WHERE part_id = ? "
        "GROUP BY location_id", (part_id,)
    ).fetchall():
        loc = r["location_id"] or default_shelf(conn)
        cur = conn.execute("UPDATE part_stock SET allocated = ? WHERE part_id = ? AND location_id = ?",
                           (r["qty"], part_id, loc))
        if cur.rowcount == 0:
            conn.execute("INSERT INTO part_stock (part_id, location_id, on_hand, allocated) VALUES (?, ?, 0, ?)",
                         (part_id, loc, r["qty"]))
    conn.execute(
        """UPDATE parts SET total_alloc = (SELECT COALESCE(SUM(pk.allocated), 0) FROM part_stock pk
             JOIN stock_locations l ON l.id = pk.location_id WHERE pk.part_id = ? AND l.kind = 'ours')
           WHERE id = ?""", (part_id, part_id))


def movements_for_part(db: Database, part_id: int, limit: int = 50) -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT m.*, l.name AS location_name, l.kind AS location_kind
               FROM stock_movements m LEFT JOIN stock_locations l ON l.id = m.location_id
               WHERE m.part_id = ? ORDER BY m.id DESC LIMIT ?""",
            (part_id, limit),
        )]
```

- [ ] **Step 5: Update callers of `_location_id` / the rollup in `repo.py`**

In `src/partpilot/web/features/catalog/repo.py`: delete `_location_id`. In `create_part` (opening block) replace `loc_id = _location_id(conn, (opening or {}).get("location_id"))` with:

```python
            from .stock import OPENING, default_shelf, post_movement
            loc_id = (opening or {}).get("location_id") or default_shelf(conn)
```

In `update_part` (stock block), the edit form only ever manages the **default own shelf's** row — never a customer row:

```python
        from .stock import ADJUST, OPENING, default_shelf, post_movement

        qty = (stock or {}).get("qty")
        loc_id = (stock or {}).get("location_id") or default_shelf(conn)
        own_row = conn.execute(
            """SELECT pk.id, pk.on_hand, pk.location_id FROM part_stock pk
               JOIN stock_locations l ON l.id = pk.location_id
               WHERE pk.part_id = ? AND l.kind = 'ours' ORDER BY pk.id LIMIT 1""", (part_id,)).fetchone()
        if own_row is not None:
            conn.execute("UPDATE part_stock SET bin=?, location_id=? WHERE id=?",
                         ((stock or {}).get("bin"), loc_id, own_row["id"]))
            delta = (qty or 0) - (own_row["on_hand"] or 0)
            if delta:
                post_movement(conn, part_id, delta=delta, mtype=ADJUST, note="part edit", location_id=loc_id)
        elif qty:
            post_movement(conn, part_id, delta=qty, mtype=OPENING, note="opening stock (part edit)",
                          location_id=loc_id)
            if (stock or {}).get("bin"):
                conn.execute("UPDATE part_stock SET bin = ? WHERE part_id = ? AND location_id = ?",
                             ((stock or {}).get("bin"), part_id, loc_id))
        from .stock import _rollup
        _rollup(conn, part_id)
        conn.commit()
```

(Remove the old trailing `UPDATE parts SET total_qty = ...` statement — `_rollup` replaces it.)

Any other `UPDATE parts SET total_qty = (SELECT COALESCE(SUM(on_hand)...` in the catalog feature (grep `SUM(on_hand)`) is replaced by `_rollup(conn, part_id)`.

- [ ] **Step 6: Router `move_stock` targets the chosen shelf**

In `src/partpilot/web/features/catalog/router.py` `move_stock`, replace the `adjust` branch and the call:

```python
    with db.connect() as conn:
        location_id = location_id or stock.default_shelf(conn)
        current = stock.shelf_on_hand(conn, part_id, location_id)
    if action == "receive" and qty:
        delta, mtype = abs(qty), stock.RECEIVE
    elif action == "issue" and qty:
        delta, mtype = -abs(qty), stock.ISSUE
    elif action == "adjust" and qty is not None:       # qty is the new on-hand target ON THAT SHELF
        delta, mtype = qty - current, stock.ADJUST
    else:
        delta = None
```

- [ ] **Step 7: Replace the duplicated allocation mirrors**

Delete `_recompute_part_alloc` from `customer_orders/repo.py` and `despatch/repo.py`; replace every call with `stock.recompute_alloc(conn, part_id)` (`customer_orders/repo.py` imports `from ..catalog import stock`; despatch already imports `stock`).

- [ ] **Step 8: Run tests**

Run: `uv run pytest tests/test_shelves_core.py -q` → Expected: 6 passed.
Run: `uv run pytest -q` → Expected: all green. Any test that called `post_movement`/`adjust_stock` without `location_id` is updated to pass `location_id=stock.default_shelf(conn)` (grep `post_movement(` in `tests/`).

- [ ] **Step 9: Commit**

```bash
git add -A && git commit -m "Shelves: strict per-shelf stock core, ours-only part totals, migration 22"
```

---

### Task 2: Shelf repo — list, pick, create, rename, retire, preferred shelf

**Files:**
- Modify: `src/partpilot/web/features/catalog/repo.py` (replace `locations`, add shelf functions)
- Test: `tests/test_shelves_repo.py`

**Interfaces:**
- Produces (all in `catalog.repo`):
  - `locations(db, *, include_retired=False, kind=None, owner_contact_id=None) -> list[dict]` rows `{id, name, kind, owner_contact_id, owner_name, customer_order_id, order_ref, retired_at, part_count, on_hand_total}`
  - `get_location(db, location_id) -> dict | None` (same shape + `value_total`)
  - `default_shelf_id(db) -> int`
  - `pickable_shelves(db, *, customer_id=None) -> list[dict]`
  - `shelves_for_customer(db, contact_id) -> list[dict]`, `shelf_for_order(db, order_id) -> dict | None`
  - `preferred_shelf_id(db, *, customer_id=None, order_id=None) -> int`
  - `candidate_shelf_ids(db, *, customer_id=None, order_id=None) -> list[int]` (job, customer, then ours)
  - `create_shelf(db, *, kind, owner_contact_id=None, customer_order_id=None, name=None) -> int`
  - `rename_shelf(db, location_id, name)`, `retire_shelf(db, location_id)`
  - `shelf_stock(db, location_id) -> list[dict]`, `shelf_movements(db, location_id, limit=100) -> list[dict]`

- [ ] **Step 1: Failing tests**

```python
# tests/test_shelves_repo.py
import pytest

from partpilot.web.core import FeatureRegistry
from partpilot.web.core.db import Database
from partpilot.web.features.assemblies import feature as assemblies_feature
from partpilot.web.features.catalog import feature as catalog_feature
from partpilot.web.features.catalog import repo as catrepo
from partpilot.web.features.catalog import stock
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
    database = Database(tmp_path / "shelves.db")
    reg = FeatureRegistry()
    reg.register(catalog_feature, assemblies_feature, contacts_feature, co_feature, wo_feature, po_feature)
    database.apply_migrations(reg)
    return database


def _customer(db, name="Acme"):
    return conrepo.create_contact(db, {"kind": "customer", "name": name, "short_name": name})


def _order(db, cust):
    return corepo.create_order(db, {"customer_id": cust})


def test_create_customer_and_job_shelves_autonamed(db):
    cust = _customer(db)
    order = _order(db, cust)
    s1 = catrepo.create_shelf(db, kind="customer", owner_contact_id=cust)
    s2 = catrepo.create_shelf(db, kind="customer", owner_contact_id=cust, customer_order_id=order)
    rows = {r["id"]: r for r in catrepo.locations(db)}
    assert rows[s1]["name"] == "Acme" and rows[s1]["kind"] == "customer" and rows[s1]["owner_name"] == "Acme"
    assert rows[s2]["name"].startswith("Acme · CO-") and rows[s2]["order_ref"] == rows[s2]["name"].split(" · ")[1]


def test_create_shelf_validation(db):
    sup = conrepo.create_contact(db, {"kind": "supplier", "name": "Digi"})
    with pytest.raises(ValueError):
        catrepo.create_shelf(db, kind="customer")                       # no owner
    with pytest.raises(ValueError):
        catrepo.create_shelf(db, kind="customer", owner_contact_id=sup)  # not a customer
    with pytest.raises(ValueError):
        catrepo.create_shelf(db, kind="ours", name="Main")               # duplicate name
    with pytest.raises(ValueError):
        catrepo.create_shelf(db, kind="elsewhere", name="X")


def test_pickable_and_preferred(db):
    acme, beta = _customer(db, "Acme"), _customer(db, "Beta")
    order = _order(db, acme)
    a_shelf = catrepo.create_shelf(db, kind="customer", owner_contact_id=acme)
    job = catrepo.create_shelf(db, kind="customer", owner_contact_id=acme, customer_order_id=order)
    catrepo.create_shelf(db, kind="customer", owner_contact_id=beta)
    main = catrepo.default_shelf_id(db)
    ids = [s["id"] for s in catrepo.pickable_shelves(db, customer_id=acme)]
    assert ids == [main, job, a_shelf]                      # ours first, then job, then customer
    assert catrepo.preferred_shelf_id(db, customer_id=acme, order_id=order) == job
    assert catrepo.preferred_shelf_id(db, customer_id=acme) == a_shelf
    assert catrepo.preferred_shelf_id(db, customer_id=beta, order_id=order) != job
    assert catrepo.preferred_shelf_id(db) == main
    assert catrepo.candidate_shelf_ids(db, customer_id=acme, order_id=order) == [job, a_shelf, main]


def test_retire_rules(db):
    cust = _customer(db)
    shelf = catrepo.create_shelf(db, kind="customer", owner_contact_id=cust)
    pid = catrepo.create_part(db, part={"part_no": "R-1"}, supplier_lines=[], opening=None)
    main = catrepo.default_shelf_id(db)
    with pytest.raises(ValueError):
        catrepo.retire_shelf(db, main)                       # last own shelf
    with db.connect() as conn:
        stock.post_movement(conn, pid, delta=5, mtype=stock.RECEIVE, location_id=shelf); conn.commit()
    with pytest.raises(ValueError, match="stock"):
        catrepo.retire_shelf(db, shelf)
    with db.connect() as conn:
        stock.post_movement(conn, pid, delta=-5, mtype=stock.ADJUST, location_id=shelf); conn.commit()
    po = porepo.create_po(db, {"supplier_id": None, "location_id": shelf})
    with pytest.raises(ValueError, match="purchase order"):
        catrepo.retire_shelf(db, shelf)
    with db.connect() as conn:
        conn.execute("UPDATE purchase_orders SET status = 'cancelled' WHERE id = ?", (po,)); conn.commit()
    catrepo.retire_shelf(db, shelf)
    assert shelf not in [s["id"] for s in catrepo.pickable_shelves(db, customer_id=cust)]
    assert shelf in [s["id"] for s in catrepo.locations(db, include_retired=True)]


def test_shelf_stock_and_value(db):
    cust = _customer(db)
    shelf = catrepo.create_shelf(db, kind="customer", owner_contact_id=cust)
    pid = catrepo.create_part(db, part={"part_no": "R-1", "unit_cost": 2.0}, supplier_lines=[], opening=None)
    with db.connect() as conn:
        stock.post_movement(conn, pid, delta=10, mtype=stock.RECEIVE, location_id=shelf); conn.commit()
    rows = catrepo.shelf_stock(db, shelf)
    assert rows[0]["part_no"] == "R-1" and rows[0]["on_hand"] == 10 and rows[0]["free"] == 10
    assert catrepo.get_location(db, shelf)["value_total"] == pytest.approx(20.0)
    assert catrepo.shelf_movements(db, shelf)[0]["qty_delta"] == 10
```

(`porepo.create_po` accepting `location_id` lands in Task 5; until then the retire test's PO branch will fail — see Step 4 note.)

- [ ] **Step 2: Run** `uv run pytest tests/test_shelves_repo.py -q` → Expected: FAIL `AttributeError: create_shelf`.

- [ ] **Step 3: Implement in `catalog/repo.py`** (replace the old `locations`):

```python
# ---- shelves (stock locations with an owner) ----
_SHELF_KINDS = ("ours", "customer")


def _table_exists(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _shelf_rows(conn, where: str, params: tuple) -> list[dict]:
    rows = [dict(r) for r in conn.execute(
        f"""SELECT l.*, 
                   (SELECT COUNT(*) FROM part_stock pk WHERE pk.location_id = l.id AND pk.on_hand != 0) AS part_count,
                   (SELECT COALESCE(SUM(pk.on_hand), 0) FROM part_stock pk WHERE pk.location_id = l.id) AS on_hand_total
            FROM stock_locations l {where}
            ORDER BY CASE l.kind WHEN 'ours' THEN 0 ELSE 1 END,
                     CASE WHEN l.customer_order_id IS NULL THEN 1 ELSE 0 END, l.id""", params)]
    has_contacts, has_orders = _table_exists(conn, "contacts"), _table_exists(conn, "customer_orders")
    for r in rows:
        r["owner_name"] = r["order_ref"] = None
        if has_contacts and r["owner_contact_id"]:
            c = conn.execute("SELECT COALESCE(NULLIF(short_name, ''), name) AS n FROM contacts WHERE id = ?",
                             (r["owner_contact_id"],)).fetchone()
            r["owner_name"] = c["n"] if c else None
        if has_orders and r["customer_order_id"]:
            o = conn.execute("SELECT order_ref FROM customer_orders WHERE id = ?", (r["customer_order_id"],)).fetchone()
            r["order_ref"] = o["order_ref"] if o else None
    return rows


def locations(db: Database, *, include_retired: bool = False, kind: str | None = None,
              owner_contact_id: int | None = None) -> list[dict]:
    where, params = [], []
    if not include_retired:
        where.append("l.retired_at IS NULL")
    if kind:
        where.append("l.kind = ?"); params.append(kind)
    if owner_contact_id:
        where.append("l.owner_contact_id = ?"); params.append(owner_contact_id)
    with db.connect() as conn:
        return _shelf_rows(conn, ("WHERE " + " AND ".join(where)) if where else "", tuple(params))


def get_location(db: Database, location_id: int) -> dict | None:
    with db.connect() as conn:
        rows = _shelf_rows(conn, "WHERE l.id = ?", (location_id,))
        if not rows:
            return None
        shelf = rows[0]
        shelf["value_total"] = conn.execute(
            "SELECT COALESCE(SUM(pk.on_hand * COALESCE(p.unit_cost, 0)), 0) FROM part_stock pk "
            "JOIN parts p ON p.id = pk.part_id WHERE pk.location_id = ?", (location_id,)).fetchone()[0]
    return shelf


def default_shelf_id(db: Database) -> int:
    from .stock import default_shelf
    with db.connect() as conn:
        sid = default_shelf(conn)
        conn.commit()
    return sid


def shelves_for_customer(db: Database, contact_id: int) -> list[dict]:
    return locations(db, kind="customer", owner_contact_id=contact_id)


def shelf_for_order(db: Database, order_id: int) -> dict | None:
    with db.connect() as conn:
        rows = _shelf_rows(conn, "WHERE l.customer_order_id = ? AND l.retired_at IS NULL", (order_id,))
    return rows[0] if rows else None


def pickable_shelves(db: Database, *, customer_id: int | None = None) -> list[dict]:
    """Own shelves first, then (if a customer is given) their job shelves, then their customer shelf."""
    ours = locations(db, kind="ours")
    if not ours:
        default_shelf_id(db); ours = locations(db, kind="ours")
    theirs = shelves_for_customer(db, customer_id) if customer_id else []
    return ours + theirs


def candidate_shelf_ids(db: Database, *, customer_id: int | None = None, order_id: int | None = None) -> list[int]:
    """Where a customer's goods may come from, most specific first: job shelf, customer shelf, ours."""
    out: list[int] = []
    job = shelf_for_order(db, order_id) if order_id else None
    if job and (customer_id is None or job["owner_contact_id"] == customer_id):
        out.append(job["id"])
    if customer_id:
        out += [s["id"] for s in shelves_for_customer(db, customer_id)
                if s["customer_order_id"] is None and s["id"] not in out]
    out += [s["id"] for s in locations(db, kind="ours") if s["id"] not in out]
    if not out:
        out.append(default_shelf_id(db))
    return out


def preferred_shelf_id(db: Database, *, customer_id: int | None = None, order_id: int | None = None) -> int:
    return candidate_shelf_ids(db, customer_id=customer_id, order_id=order_id)[0]


def create_shelf(db: Database, *, kind: str, owner_contact_id: int | None = None,
                 customer_order_id: int | None = None, name: str | None = None) -> int:
    if kind not in _SHELF_KINDS:
        raise ValueError("A shelf is either ours or a customer's.")
    name = (name or "").strip()
    with db.connect() as conn:
        if kind == "customer":
            if not owner_contact_id:
                raise ValueError("A customer shelf needs a customer.")
            owner = conn.execute("SELECT kind, COALESCE(NULLIF(short_name, ''), name) AS n FROM contacts WHERE id = ?",
                                 (owner_contact_id,)).fetchone()
            if owner is None or owner["kind"] != "customer":
                raise ValueError("The shelf owner must be a customer contact.")
            order_ref = None
            if customer_order_id:
                o = conn.execute("SELECT customer_id, order_ref FROM customer_orders WHERE id = ?",
                                 (customer_order_id,)).fetchone()
                if o is None or o["customer_id"] != owner_contact_id:
                    raise ValueError("That order does not belong to this customer.")
                order_ref = o["order_ref"] or f"CO-{customer_order_id:05d}"
            if not name:
                name = f"{owner['n']} · {order_ref}" if order_ref else owner["n"]
        else:
            owner_contact_id = customer_order_id = None
            if not name:
                raise ValueError("Give the shelf a name.")
        if conn.execute("SELECT 1 FROM stock_locations WHERE name = ?", (name,)).fetchone():
            raise ValueError(f"A shelf called {name!r} already exists.")
        sid = conn.execute(
            "INSERT INTO stock_locations (name, kind, owner_contact_id, customer_order_id) VALUES (?, ?, ?, ?)",
            (name, kind, owner_contact_id, customer_order_id)).lastrowid
        conn.commit()
    return sid


def rename_shelf(db: Database, location_id: int, name: str) -> None:
    name = (name or "").strip()
    if not name:
        raise ValueError("Give the shelf a name.")
    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM stock_locations WHERE name = ? AND id != ?", (name, location_id)).fetchone():
            raise ValueError(f"A shelf called {name!r} already exists.")
        conn.execute("UPDATE stock_locations SET name = ? WHERE id = ?", (name, location_id))
        conn.commit()


def retire_shelf(db: Database, location_id: int) -> None:
    with db.connect() as conn:
        shelf = conn.execute("SELECT * FROM stock_locations WHERE id = ?", (location_id,)).fetchone()
        if shelf is None:
            raise ValueError("Shelf not found.")
        if shelf["kind"] == "ours" and conn.execute(
                "SELECT COUNT(*) FROM stock_locations WHERE kind = 'ours' AND retired_at IS NULL").fetchone()[0] <= 1:
            raise ValueError("The last shelf of our own cannot be retired.")
        if conn.execute("SELECT 1 FROM part_stock WHERE location_id = ? AND on_hand != 0 LIMIT 1", (location_id,)).fetchone():
            raise ValueError("The shelf still holds stock — it cannot be retired.")
        if _table_exists(conn, "purchase_orders") and conn.execute(
                "SELECT 1 FROM purchase_orders WHERE location_id = ? AND status IN ('draft','ordered') LIMIT 1",
                (location_id,)).fetchone():
            raise ValueError("An open purchase order still receives into this shelf.")
        if _table_exists(conn, "work_orders") and conn.execute(
                "SELECT 1 FROM work_orders WHERE location_id = ? AND status IN ('allocated','issued') LIMIT 1",
                (location_id,)).fetchone():
            raise ValueError("An open work order still builds from this shelf.")
        conn.execute("UPDATE stock_locations SET retired_at = datetime('now') WHERE id = ?", (location_id,))
        conn.commit()


def shelf_stock(db: Database, location_id: int) -> list[dict]:
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            """SELECT pk.part_id, p.part_no, p.value, pk.on_hand, pk.allocated, pk.bin,
                      COALESCE(p.unit_cost, 0) AS unit_cost
               FROM part_stock pk JOIN parts p ON p.id = pk.part_id
               WHERE pk.location_id = ? AND (pk.on_hand != 0 OR pk.allocated != 0)
               ORDER BY p.part_no""", (location_id,))]
    for r in rows:
        r["free"] = (r["on_hand"] or 0) - (r["allocated"] or 0)
        r["line_value"] = (r["on_hand"] or 0) * r["unit_cost"]
    return rows


def shelf_movements(db: Database, location_id: int, limit: int = 100) -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT m.*, p.part_no FROM stock_movements m JOIN parts p ON p.id = m.part_id
               WHERE m.location_id = ? ORDER BY m.id DESC LIMIT ?""", (location_id, limit))]
```

`purchase_orders.location_id` does not exist until Task 5; `retire_shelf` guards with `_table_exists` but the column check will raise `sqlite3.OperationalError` on a DB with PO migrations < 5. Order Task 5's migration **before** running `test_retire_rules`' PO branch: do Task 5 Step 3 (migration only) now if the test errors, and note the ruling in the ledger.

- [ ] **Step 4: Run** `uv run pytest tests/test_shelves_repo.py -q` → Expected: PASS (5). Then `uv run pytest -q` → green.
- [ ] **Step 5: Commit** `git add -A && git commit -m "Shelves: repo — list/pick/create/rename/retire, preferred and candidate shelves"`

---

### Task 3: Shelf pages, Setup tile, contact panel, order job-shelf line

**Files:**
- Create: `src/partpilot/web/core/templates/_shelf_select.html`
- Create: `src/partpilot/web/features/catalog/templates/shelves.html`, `shelf.html`
- Modify: `src/partpilot/web/features/catalog/router.py` (routes **before** `/{part_id}`)
- Modify: `src/partpilot/web/features/setup/router.py` TOOLS; `contacts/router.py` + `contact_detail.html`; `customer_orders/router.py` `_render_detail` + `customer_order_detail.html`
- Test: `tests/test_shelves_web.py`

**Interfaces:**
- Produces: routes `GET /catalog/shelves`, `GET /catalog/shelves/{id}`, `POST /catalog/shelves` (fields `kind, owner_contact_id, customer_order_id, name, return_to`), `POST /catalog/shelves/{id}/rename`, `POST /catalog/shelves/{id}/retire`.
- Produces: include `_shelf_select.html` expecting context `shelves` (list), `shelf_selected` (id or `'none'`), `shelf_field` (input name, default `location_id`), `shelf_none` (bool).

- [ ] **Step 1: Failing tests**

```python
# tests/test_shelves_web.py
import pytest
from fastapi.testclient import TestClient

import partpilot.web.app as web_app
from partpilot.web.features.catalog import repo as catrepo
from partpilot.web.features.contacts import repo as conrepo
from partpilot.web.features.customer_orders import repo as corepo


@pytest.fixture
def app(tmp_path):
    a = web_app.create_app(db_path=tmp_path / "p.db", data_dir=tmp_path / "data", secret_key="s")
    a.state.store.create_user("boss", "pw", role="admin")
    a.state.store.create_user("ship", "pw", role="shipping")
    return a


def _client(app, user="boss"):
    c = TestClient(app)
    c.post("/login", data={"username": user, "password": "pw"})
    return c


def test_create_customer_shelf_from_contact_page(app):
    db = app.state.database
    cust = conrepo.create_contact(db, {"kind": "customer", "name": "Acme"})
    c = _client(app)
    assert "New shelf" in c.get(f"/contacts/{cust}").text
    r = c.post("/catalog/shelves", data={"kind": "customer", "owner_contact_id": cust,
                                          "return_to": f"/contacts/{cust}"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/contacts/{cust}"
    shelves = catrepo.shelves_for_customer(db, cust)
    assert len(shelves) == 1 and shelves[0]["name"] == "Acme"
    assert "Acme" in c.get(f"/contacts/{cust}").text.split("Stock shelves")[1]


def test_create_job_shelf_from_order_page(app):
    db = app.state.database
    cust = conrepo.create_contact(db, {"kind": "customer", "name": "Acme"})
    order = corepo.create_order(db, {"customer_id": cust})
    c = _client(app)
    assert "Create job shelf" in c.get(f"/customer-orders/{order}").text
    c.post("/catalog/shelves", data={"kind": "customer", "owner_contact_id": cust,
                                      "customer_order_id": order, "return_to": f"/customer-orders/{order}"})
    assert catrepo.shelf_for_order(db, order) is not None
    html = c.get(f"/customer-orders/{order}").text
    assert "Create job shelf" not in html and "Acme · CO-" in html


def test_shelves_list_and_detail_and_retire(app):
    db = app.state.database
    cust = conrepo.create_contact(db, {"kind": "customer", "name": "Acme"})
    sid = catrepo.create_shelf(db, kind="customer", owner_contact_id=cust)
    c = _client(app)
    html = c.get("/catalog/shelves").text
    assert "Main" in html and "Acme" in html and "Stock shelves" in html
    assert "Acme" in c.get(f"/catalog/shelves/{sid}").text
    r = c.post(f"/catalog/shelves/{sid}/retire", follow_redirects=False)
    assert r.status_code == 303
    assert catrepo.get_location(db, sid)["retired_at"]


def test_bad_return_to_is_ignored_and_shipping_cannot_create(app):
    db = app.state.database
    cust = conrepo.create_contact(db, {"kind": "customer", "name": "Acme"})
    c = _client(app)
    r = c.post("/catalog/shelves", data={"kind": "customer", "owner_contact_id": cust,
                                          "return_to": "https://evil.example/"}, follow_redirects=False)
    assert r.headers["location"].startswith("/catalog/shelves/")
    assert _client(app, "ship").post("/catalog/shelves", data={"kind": "ours", "name": "X"}).status_code == 403


def test_setup_has_shelves_tile(app):
    assert "/catalog/shelves" in _client(app).get("/setup").text
```

- [ ] **Step 2: Run** → Expected: FAIL (404 on `/catalog/shelves`, "New shelf" missing).

- [ ] **Step 3: Shared picker partial** `src/partpilot/web/core/templates/_shelf_select.html`

```jinja
{# Shelf picker. Context: shelves (list of {id,name,kind}), shelf_selected (id | 'none'),
   shelf_field (input name; default location_id), shelf_none (offer "Don't check stock"). #}
{% set _f = shelf_field or 'location_id' %}
<select id="{{ _f }}" name="{{ _f }}">
  {% if shelf_none %}<option value="none" {{ 'selected' if shelf_selected == 'none' else '' }}>Don't check stock</option>{% endif %}
  {% for s in shelves %}
  <option value="{{ s.id }}" {{ 'selected' if shelf_selected|string == s.id|string else '' }}>{% if s.kind == 'customer' %}Customer: {% endif %}{{ s.name }}</option>
  {% endfor %}
</select>
```

- [ ] **Step 4: Routes in `catalog/router.py`** — insert *above* `@router.get("/{part_id}")`:

```python
from ...auth import PURCHASE_ROLES
from urllib.parse import urlparse

SHELF_ADMIN_ROLES = frozenset({"admin"})


def _safe_return(url: str | None, fallback: str) -> str:
    u = (url or "").strip()
    return u if u.startswith("/") and not u.startswith("//") and urlparse(u).netloc == "" else fallback


@router.get("/shelves", response_class=HTMLResponse)
def shelves_list(request: Request):
    user = require_user(request)
    db = request.app.state.database
    return request.app.state.templates.TemplateResponse(
        request, "shelves.html",
        {"shelves": repo.locations(db, include_retired=True), "error": None,
         "can_admin": user.role in SHELF_ADMIN_ROLES})


@router.get("/shelves/{location_id}", response_class=HTMLResponse)
def shelf_detail(request: Request, location_id: int, error: str | None = None):
    user = require_user(request)
    db = request.app.state.database
    shelf = repo.get_location(db, location_id)
    if shelf is None:
        return request.app.state.templates.TemplateResponse(
            request, "error.html", {"message": "Shelf not found."}, status_code=404)
    return request.app.state.templates.TemplateResponse(
        request, "shelf.html",
        {"shelf": shelf, "rows": repo.shelf_stock(db, location_id),
         "movements": repo.shelf_movements(db, location_id), "error": error,
         "can_admin": user.role in SHELF_ADMIN_ROLES, "can_move": user.role in STOCK_MOVE_ROLES},
        status_code=400 if error else 200)


@router.post("/shelves")
async def shelf_create(request: Request):
    require_role(request, PURCHASE_ROLES)
    db = request.app.state.database
    form = await request.form()
    try:
        sid = repo.create_shelf(
            db, kind=(form.get("kind") or "").strip(), owner_contact_id=_int(form.get("owner_contact_id")),
            customer_order_id=_int(form.get("customer_order_id")), name=form.get("name"))
    except ValueError as exc:
        back = _safe_return(form.get("return_to"), "/catalog/shelves")
        sep = "&" if "?" in back else "?"
        return RedirectResponse(f"{back}{sep}shelf_error={exc}", status_code=303)
    return RedirectResponse(_safe_return(form.get("return_to"), f"/catalog/shelves/{sid}"), status_code=303)


@router.post("/shelves/{location_id}/rename")
async def shelf_rename(request: Request, location_id: int):
    require_role(request, SHELF_ADMIN_ROLES)
    form = await request.form()
    try:
        repo.rename_shelf(request.app.state.database, location_id, form.get("name"))
    except ValueError as exc:
        return shelf_detail(request, location_id, error=str(exc))
    return RedirectResponse(f"/catalog/shelves/{location_id}", status_code=303)


@router.post("/shelves/{location_id}/retire")
def shelf_retire(request: Request, location_id: int):
    require_role(request, SHELF_ADMIN_ROLES)
    try:
        repo.retire_shelf(request.app.state.database, location_id)
    except ValueError as exc:
        return shelf_detail(request, location_id, error=str(exc))
    return RedirectResponse("/catalog/shelves", status_code=303)
```

The contact and order pages read `shelf_error` from the query string and show it in `.err` (Step 7).

- [ ] **Step 5: `shelves.html`**

```jinja
{% extends "base.html" %}
{% block title %}Stock shelves · PartPilot{% endblock %}
{% block content %}
<div class="panel">
  <p class="note"><a href="/setup">← Setup &amp; Tools</a></p>
  <h1>Stock shelves</h1>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <p class="note">A shelf is where stock sits and <b>whose</b> it is. <b>Our</b> shelves count in our totals,
    valuation and the webshop. A <b>customer</b> shelf holds parts the customer paid for: only their work orders
    draw from it, and it never counts as ours. Customer shelves are created from the customer's contact page
    or from a customer order (job shelf).</p>
  <div style="overflow-x:auto;">
    <table>
      <thead><tr><th>Shelf</th><th>Kind</th><th>Owner</th><th>Order</th><th class="num">Parts</th>
                 <th class="num">On hand</th><th></th></tr></thead>
      <tbody>
        {% for s in shelves %}
        <tr>
          <td><b>{{ s.name }}</b>{% if s.retired_at %} <span class="badge muted">retired</span>{% endif %}</td>
          <td>{% if s.kind == 'customer' %}<span class="badge warn">customer</span>{% else %}<span class="badge stock">ours</span>{% endif %}</td>
          <td>{% if s.owner_contact_id %}<a href="/contacts/{{ s.owner_contact_id }}">{{ s.owner_name or '#' ~ s.owner_contact_id }}</a>{% endif %}</td>
          <td>{% if s.customer_order_id %}<a href="/customer-orders/{{ s.customer_order_id }}">{{ s.order_ref or '#' ~ s.customer_order_id }}</a>{% endif %}</td>
          <td class="num">{{ s.part_count }}</td>
          <td class="num">{{ "%g"|format(s.on_hand_total) }}</td>
          <td><button type="button" class="ghost" onclick="window.location.href='/catalog/shelves/{{ s.id }}'">Open</button></td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% if can_admin %}
  <h2 style="font-size:1.05rem; margin-top:1.4rem;">New shelf of our own</h2>
  <form method="post" action="/catalog/shelves" style="display:flex; gap:.6rem; align-items:flex-end; flex-wrap:wrap;">
    <input type="hidden" name="kind" value="ours">
    <input type="hidden" name="return_to" value="/catalog/shelves">
    <div><label for="name">Name</label><input id="name" name="name" type="text" required placeholder="e.g. Store 2"></div>
    <button type="submit" class="ghost">Create</button>
  </form>
  {% endif %}
</div>
{% endblock %}
```

- [ ] **Step 6: `shelf.html`**

```jinja
{% extends "base.html" %}
{% block title %}{{ shelf.name }} · Shelf · PartPilot{% endblock %}
{% block content %}
<div class="panel">
  <p class="note"><a href="/catalog/shelves">← Stock shelves</a></p>
  <h1>{{ shelf.name }}
    {% if shelf.kind == 'customer' %}<span class="badge warn">customer shelf</span>{% else %}<span class="badge stock">our shelf</span>{% endif %}
    {% if shelf.retired_at %}<span class="badge muted">retired</span>{% endif %}</h1>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <p class="note">
    {% if shelf.owner_contact_id %}Owner: <a href="/contacts/{{ shelf.owner_contact_id }}">{{ shelf.owner_name }}</a>{% endif %}
    {% if shelf.customer_order_id %} · Job: <a href="/customer-orders/{{ shelf.customer_order_id }}">{{ shelf.order_ref }}</a>{% endif %}
  </p>
  <div class="cards">
    <div class="card"><b>{{ rows|length }}</b><small>parts</small></div>
    <div class="card"><b>{{ "%g"|format(shelf.on_hand_total) }}</b><small>units on hand</small></div>
    <div class="card"><b>{{ "%.2f"|format(shelf.value_total) }}</b><small>value at unit cost</small></div>
  </div>
  <div style="overflow-x:auto; margin-top:1rem;">
    <table>
      <thead><tr><th>Part</th><th>Value</th><th>Bin</th><th class="num">On hand</th><th class="num">Allocated</th>
                 <th class="num">Free</th><th class="num">Line value</th>{% if can_move %}<th></th>{% endif %}</tr></thead>
      <tbody>
        {% for r in rows %}
        <tr>
          <td><a href="/catalog/{{ r.part_id }}">{{ r.part_no }}</a></td>
          <td class="note">{{ r.value or '' }}</td><td>{{ r.bin or '' }}</td>
          <td class="num">{% if r.on_hand < 0 %}<span class="badge bad">{{ "%g"|format(r.on_hand) }}</span>{% else %}{{ "%g"|format(r.on_hand) }}{% endif %}</td>
          <td class="num">{{ "%g"|format(r.allocated) }}</td><td class="num">{{ "%g"|format(r.free) }}</td>
          <td class="num">{{ "%.2f"|format(r.line_value) }}</td>
          {% if can_move %}<td><button type="button" class="ghost" onclick="window.location.href='/catalog/{{ r.part_id }}#adjust'">Adjust</button></td>{% endif %}
        </tr>
        {% endfor %}
        {% if not rows %}<tr><td colspan="8" class="note">Nothing on this shelf.</td></tr>{% endif %}
      </tbody>
    </table>
  </div>
  {% if can_admin and not shelf.retired_at %}
  <div style="margin-top:1.2rem; display:flex; gap:.8rem; align-items:flex-end; flex-wrap:wrap;">
    <form method="post" action="/catalog/shelves/{{ shelf.id }}/rename" style="display:flex; gap:.6rem; align-items:flex-end;">
      <div><label for="name">Rename</label><input id="name" name="name" type="text" value="{{ shelf.name }}" required></div>
      <button type="submit" class="ghost">Save</button>
    </form>
    <form method="post" action="/catalog/shelves/{{ shelf.id }}/retire"
          onsubmit="return confirm('Retire this shelf? It must be empty with no open orders.')">
      <button type="submit" class="danger">Retire shelf</button>
    </form>
  </div>
  {% endif %}
  <h2 style="font-size:1.05rem; margin-top:1.4rem;">Recent movements</h2>
  <div style="overflow-x:auto;">
    <table>
      <thead><tr><th>When</th><th>Part</th><th>Type</th><th class="num">Δ</th><th class="num">After</th><th>Ref</th><th>Note</th><th>User</th></tr></thead>
      <tbody>
        {% for m in movements %}
        <tr><td class="note">{{ m.moved_at }}</td><td><a href="/catalog/{{ m.part_id }}">{{ m.part_no }}</a></td>
            <td>{{ m.mtype }}</td><td class="num">{{ "%g"|format(m.qty_delta) }}</td><td class="num">{{ "%g"|format(m.qty_after) }}</td>
            <td>{{ m.reference or '' }}</td><td class="note">{{ m.note or '' }}</td><td class="note">{{ m.user or '' }}</td></tr>
        {% endfor %}
        {% if not movements %}<tr><td colspan="8" class="note">No movements yet.</td></tr>{% endif %}
      </tbody>
    </table>
  </div>
</div>
{% endblock %}
```

- [ ] **Step 7: Setup tile, contact panel, order job-shelf line**

Setup `TOOLS` (after "Import parts from BOM"):

```python
    {"label": "Stock shelves", "url": "/catalog/shelves", "icon": "🗄️",
     "description": "Our shelves and customer-owned shelves: who owns what stock, where it sits, "
                    "and what it is worth. Create, rename and retire shelves."},
```

`contacts/router.py` `contact_detail`: add `"shelves": catalog_repo.shelves_for_customer(db, contact_id) if c["kind"] == "customer" else []`, `"can_shelf": user.role in PURCHASE_ROLES`, `"shelf_error": request.query_params.get("shelf_error")` (import `from ..catalog import repo as catalog_repo` and `from ...auth import PURCHASE_ROLES`). In `contact_detail.html`, before the addresses `<h2>`:

```jinja
  {% if c.kind == 'customer' %}
  <h2 style="font-size:1.05rem; margin-top:1.4rem;">Stock shelves</h2>
  {% if shelf_error %}<div class="err">{{ shelf_error }}</div>{% endif %}
  <p class="note">Parts this customer has paid for live on their own shelves. Only their work orders draw from them.</p>
  <table>
    <thead><tr><th>Shelf</th><th>Job</th><th class="num">Parts</th><th class="num">On hand</th><th></th></tr></thead>
    <tbody>
      {% for s in shelves %}
      <tr><td><b>{{ s.name }}</b></td>
          <td>{% if s.customer_order_id %}<a href="/customer-orders/{{ s.customer_order_id }}">{{ s.order_ref }}</a>{% else %}<span class="note">all jobs</span>{% endif %}</td>
          <td class="num">{{ s.part_count }}</td><td class="num">{{ "%g"|format(s.on_hand_total) }}</td>
          <td><button type="button" class="ghost" onclick="window.location.href='/catalog/shelves/{{ s.id }}'">Open</button></td></tr>
      {% endfor %}
      {% if not shelves %}<tr><td colspan="5" class="note">No shelves yet.</td></tr>{% endif %}
    </tbody>
  </table>
  {% if can_shelf %}
  <form method="post" action="/catalog/shelves" style="display:flex; gap:.6rem; align-items:flex-end; margin-top:.6rem;">
    <input type="hidden" name="kind" value="customer">
    <input type="hidden" name="owner_contact_id" value="{{ c.id }}">
    <input type="hidden" name="return_to" value="/contacts/{{ c.id }}">
    <div><label for="shelf_name">Shelf name (optional)</label><input id="shelf_name" name="name" type="text" placeholder="{{ c.short_name or c.name }}"></div>
    <button type="submit" class="ghost">New shelf</button>
  </form>
  {% endif %}
  {% endif %}
```

`customer_orders/router.py` `_render_detail`: add `"job_shelf": catalog_repo.shelf_for_order(db, order_id)`, `"can_shelf": user.role in PURCHASE_ROLES`, `"shelf_error": request.query_params.get("shelf_error")`. In `customer_order_detail.html`, directly under the header `.note` block (before the action bar):

```jinja
  {% if shelf_error %}<div class="err">{{ shelf_error }}</div>{% endif %}
  <p class="note">Job shelf:
    {% if job_shelf %}<a href="/catalog/shelves/{{ job_shelf.id }}"><b>{{ job_shelf.name }}</b></a>
      <span class="badge warn">customer-owned</span>
    {% elif can_shelf and o.customer_id %}
      <form method="post" action="/catalog/shelves" style="display:inline;">
        <input type="hidden" name="kind" value="customer">
        <input type="hidden" name="owner_contact_id" value="{{ o.customer_id }}">
        <input type="hidden" name="customer_order_id" value="{{ o.id }}">
        <input type="hidden" name="return_to" value="/customer-orders/{{ o.id }}">
        <button type="submit" class="ghost">Create job shelf</button>
      </form>
      <span class="note">— parts bought for this order only</span>
    {% else %}none{% endif %}
  </p>
```

(Confirm `o.customer_id` is in `get_order`'s SELECT; add it if missing.)

- [ ] **Step 8: Run** `uv run pytest tests/test_shelves_web.py -q` → PASS (5); `uv run pytest -q` → green.
- [ ] **Step 9: Commit** `git add -A && git commit -m "Shelves: list/detail pages, create from contact and order, Setup tile"`

---

### Task 4: Part page — stock by shelf, ours/customer totals, shelf-targeted adjust

**Files:**
- Modify: `catalog/repo.py` `get_part` (stock rows + totals), `catalog/router.py` `part_detail`, `templates/part_detail.html` (stock panel + adjust form), `templates/part_form.html` (location picker = own shelves only)
- Test: `tests/test_shelves_part_page.py`

- [ ] **Step 1: Failing tests**

```python
# tests/test_shelves_part_page.py
import pytest
from fastapi.testclient import TestClient

import partpilot.web.app as web_app
from partpilot.web.features.catalog import repo as catrepo
from partpilot.web.features.catalog import stock
from partpilot.web.features.contacts import repo as conrepo


@pytest.fixture
def app(tmp_path):
    a = web_app.create_app(db_path=tmp_path / "p.db", data_dir=tmp_path / "data", secret_key="s")
    a.state.store.create_user("boss", "pw", role="admin")
    return a


def _c(app):
    c = TestClient(app); c.post("/login", data={"username": "boss", "password": "pw"}); return c


def _two_shelves(db):
    cust = conrepo.create_contact(db, {"kind": "customer", "name": "Acme"})
    shelf = catrepo.create_shelf(db, kind="customer", owner_contact_id=cust)
    pid = catrepo.create_part(db, part={"part_no": "R-1"}, supplier_lines=[], opening={"qty": 100})
    with db.connect() as conn:
        stock.post_movement(conn, pid, delta=25, mtype=stock.RECEIVE, location_id=shelf); conn.commit()
    return pid, shelf


def test_part_page_shows_per_shelf_rows_and_totals(app):
    pid, shelf = _two_shelves(app.state.database)
    part = catrepo.get_part(app.state.database, pid)
    assert part["stock_ours"] == 100 and part["stock_customer"] == 25
    kinds = {s["location_name"]: s["kind"] for s in part["stock"]}
    assert kinds == {"Main": "ours", "Acme": "customer"}
    html = _c(app).get(f"/catalog/{pid}").text
    assert "Ours: 100" in html and "Customer-owned: 25" in html


def test_adjust_targets_selected_shelf(app):
    db = app.state.database
    pid, shelf = _two_shelves(db)
    _c(app).post(f"/catalog/{pid}/stock/move", data={"action": "adjust", "qty": "30", "location_id": shelf})
    with db.connect() as conn:
        assert stock.shelf_on_hand(conn, pid, shelf) == 30
        assert stock.shelf_on_hand(conn, pid, stock.default_shelf(conn)) == 100


def test_part_edit_never_rehomes_customer_row(app):
    db = app.state.database
    pid, shelf = _two_shelves(db)
    catrepo.update_part(db, pid, part={"part_no": "R-1"}, supplier_lines=[],
                        stock={"qty": 120, "location_id": None, "bin": "A1"})
    with db.connect() as conn:
        assert stock.shelf_on_hand(conn, pid, shelf) == 25
        assert stock.shelf_on_hand(conn, pid, stock.default_shelf(conn)) == 120
```

- [ ] **Step 2: Run** → FAIL (`KeyError: 'stock_ours'`).
- [ ] **Step 3: Implement** — `get_part` stock query becomes:

```python
        stock = [dict(r) for r in conn.execute(
            """SELECT pk.*, l.name AS location_name, l.kind, l.owner_contact_id, l.retired_at
               FROM part_stock pk LEFT JOIN stock_locations l ON l.id = pk.location_id
               WHERE pk.part_id = ? ORDER BY CASE l.kind WHEN 'ours' THEN 0 ELSE 1 END, pk.id""",
            (part_id,))]
    ...
    part["stock"] = stock
    part["stock_ours"] = sum((s["on_hand"] or 0) for s in stock if s.get("kind") == "ours")
    part["stock_customer"] = sum((s["on_hand"] or 0) for s in stock if s.get("kind") == "customer")
```

`part_detail` passes `"locations": repo.locations(db)` (active, all kinds — the adjust form may target a customer shelf) and `part_form` routes pass `repo.locations(db, kind="ours")`. In `part_detail.html` "Stock by location" → "Stock by shelf": add a kind badge column (`<span class="badge warn">customer</span>` / `<span class="badge stock">ours</span>`), red badge on negative on-hand, and a totals line `<p class="note">Ours: {{ "%g"|format(part.stock_ours) }} · Customer-owned: {{ "%g"|format(part.stock_customer) }}</p>`; give the adjust form `id="adjust"` and relabel the adjust help "new on-hand on the selected shelf"; the location select uses `{% with shelves=locations, shelf_selected=none, shelf_field='location_id' %}{% include "_shelf_select.html" %}{% endwith %}`.

- [ ] **Step 4: Run** `uv run pytest tests/test_shelves_part_page.py tests/test_catalog.py -q` → PASS; full suite green.
- [ ] **Step 5: Commit** `git add -A && git commit -m "Part page: stock by shelf with ours/customer totals; adjust targets the chosen shelf"`

---

### Task 5: Purchase orders receive into a shelf; shortages and suggestions per shelf

**Files:**
- Modify: `purchase_orders/migrations.py` (v5), `repo.py` (`create_po`, `get_po`, `list_pos`, `receive_po`, `shortage_suggestions`, `shortage_suggestions_grouped`, `create_pos_from_suggestions`), `router.py` (`new_form`, `create`, `suggestions_apply`), `export.py` (shelf on PDF/CSV), templates `purchase_order_form.html`, `purchase_order_detail.html`, `purchase_order_suggestions.html`
- Test: `tests/test_shelves_purchasing.py`

**Interfaces:**
- Produces: `create_po(db, data)` honours `data["location_id"]` (default own shelf); `get_po` rows carry `location_id, location_name, location_kind`; `shortage_suggestions(db)` rows carry `location_id, location_name, location_kind`; `shortage_suggestions_grouped(db) -> [{location_id, location_name, location_kind, suppliers: [{supplier_id, supplier_name, lines}]}]`; `create_pos_from_suggestions(db, selections: dict[tuple[int, int], float], user=None)`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_shelves_purchasing.py
import pytest

from partpilot.web.core import FeatureRegistry
from partpilot.web.core.db import Database
from partpilot.web.features.assemblies import feature as assemblies_feature
from partpilot.web.features.assemblies import repo as asmrepo
from partpilot.web.features.catalog import feature as catalog_feature
from partpilot.web.features.catalog import repo as catrepo
from partpilot.web.features.catalog import stock
from partpilot.web.features.contacts import feature as contacts_feature
from partpilot.web.features.contacts import repo as conrepo
from partpilot.web.features.customer_orders import feature as co_feature
from partpilot.web.features.purchase_orders import feature as po_feature
from partpilot.web.features.purchase_orders import repo
from partpilot.web.features.work_orders import feature as wo_feature
from partpilot.web.features.work_orders import repo as worepo


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "po.db")
    reg = FeatureRegistry()
    reg.register(catalog_feature, assemblies_feature, contacts_feature, co_feature, wo_feature, po_feature)
    database.apply_migrations(reg)
    return database


def _world(db):
    """R-1: 3000 on Main. PROD uses 1×R-1. Acme has a customer shelf (empty)."""
    r1 = catrepo.create_part(db, part={"part_no": "R-1"},
                             supplier_lines=[{"supplier_name": "Acme Supplies", "supplier_pno": "A-1",
                                              "unit_price": 0.5, "reel_qty": 1, "is_default": True}],
                             opening={"qty": 3000})
    asm = asmrepo.create_assembly(db, {"part_no": "PROD"})
    asmrepo.add_bom_line(db, asm, r1, 1, None)
    cust = conrepo.create_contact(db, {"kind": "customer", "name": "Acme"})
    shelf = catrepo.create_shelf(db, kind="customer", owner_contact_id=cust)
    return r1, asm, shelf


def test_shortage_is_per_shelf(db):
    r1, asm, shelf = _world(db)
    worepo.create_work_order(db, {"assembly_id": asm, "qty": 100, "location_id": shelf})
    sugg = repo.shortage_suggestions(db)
    assert len(sugg) == 1
    assert sugg[0]["location_id"] == shelf and sugg[0]["free"] == 0 and sugg[0]["short"] == 100
    # An open PO into Main does not cover the customer shelf.
    po = repo.create_po(db, {"supplier_id": sugg[0]["supplier_id"]})
    repo.add_line(db, po, r1, 100, 0.5)
    with db.connect() as conn:
        conn.execute("UPDATE purchase_orders SET status = 'ordered' WHERE id = ?", (po,)); conn.commit()
    assert repo.shortage_suggestions(db)[0]["short"] == 100


def test_suggestions_grouped_by_shelf_then_supplier_and_create_pos(db):
    r1, asm, shelf = _world(db)
    main = catrepo.default_shelf_id(db)
    worepo.create_work_order(db, {"assembly_id": asm, "qty": 100, "location_id": shelf})
    worepo.create_work_order(db, {"assembly_id": asm, "qty": 4000, "location_id": main})   # 1000 short on Main
    groups = repo.shortage_suggestions_grouped(db)
    assert [g["location_id"] for g in groups] == [main, shelf]
    assert groups[1]["suppliers"][0]["lines"][0]["short"] == 100
    ids = repo.create_pos_from_suggestions(db, {(r1, shelf): 100, (r1, main): 1000})
    assert len(ids) == 2
    by_loc = {repo.get_po(db, i)["location_id"]: repo.get_po(db, i) for i in ids}
    assert by_loc[shelf]["lines"][0]["qty"] == 100 and by_loc[main]["lines"][0]["qty"] == 1000


def test_receive_lands_on_po_shelf_not_main(db):
    r1, asm, shelf = _world(db)
    po = repo.create_po(db, {"supplier_id": None, "location_id": shelf})
    repo.add_line(db, po, r1, 50, 0.4)
    with db.connect() as conn:
        conn.execute("UPDATE purchase_orders SET status = 'ordered' WHERE id = ?", (po,)); conn.commit()
    line_id = repo.get_po(db, po)["lines"][0]["id"]
    repo.receive_po(db, po, {line_id: 50})
    with db.connect() as conn:
        assert stock.shelf_on_hand(conn, r1, shelf) == 50
        assert stock.shelf_on_hand(conn, r1, catrepo.default_shelf_id(db)) == 3000
        p = conn.execute("SELECT total_qty, customer_qty FROM parts WHERE id = ?", (r1,)).fetchone()
    assert p["total_qty"] == 3000 and p["customer_qty"] == 50


def test_receive_legacy_po_without_shelf_lands_on_main(db):
    r1, asm, shelf = _world(db)
    po = repo.create_po(db, {"supplier_id": None})
    repo.add_line(db, po, r1, 5, 0.4)
    with db.connect() as conn:
        conn.execute("UPDATE purchase_orders SET status = 'ordered', location_id = NULL WHERE id = ?", (po,)); conn.commit()
    line_id = repo.get_po(db, po)["lines"][0]["id"]
    repo.receive_po(db, po, {line_id: 5})
    with db.connect() as conn:
        assert stock.shelf_on_hand(conn, r1, catrepo.default_shelf_id(db)) == 3005


def test_receive_into_retired_shelf_refused(db):
    r1, asm, shelf = _world(db)
    po = repo.create_po(db, {"supplier_id": None, "location_id": shelf})
    repo.add_line(db, po, r1, 5, 0.4)
    with db.connect() as conn:
        conn.execute("UPDATE purchase_orders SET status = 'ordered' WHERE id = ?", (po,))
        conn.execute("UPDATE stock_locations SET retired_at = datetime('now') WHERE id = ?", (shelf,)); conn.commit()
    line_id = repo.get_po(db, po)["lines"][0]["id"]
    with pytest.raises(ValueError, match="retired"):
        repo.receive_po(db, po, {line_id: 5})
```

- [ ] **Step 2: Run** → FAIL (`location_id` unknown / KeyError).
- [ ] **Step 3: Migration 5** (`purchase_orders/migrations.py`):

```python
    Migration(
        version=5,
        name="receive-into shelf",
        sql="""
        -- The shelf goods on this PO are received into (stock_locations). NULL on legacy rows = our
        -- default shelf; new POs always get one.
        ALTER TABLE purchase_orders ADD COLUMN location_id INTEGER REFERENCES stock_locations(id);
        """,
    ),
```

- [ ] **Step 4: Repo changes**

`create_po`: add `location_id` to the INSERT; `data.get("location_id") or stock.default_shelf(conn)`.
`get_po` / `list_pos`: `LEFT JOIN stock_locations loc ON loc.id = p.location_id` and select `loc.name AS location_name, loc.kind AS location_kind` (and `p.location_id` — already in `p.*`).
`receive_po`: before posting,

```python
        loc_id = po["location_id"] or stock.default_shelf(conn)
        shelf = conn.execute("SELECT retired_at FROM stock_locations WHERE id = ?", (loc_id,)).fetchone()
        if shelf is None or shelf["retired_at"]:
            raise ValueError("This PO receives into a retired shelf — reassign it before receiving.")
```
and `stock.post_movement(..., location_id=loc_id)`.

`shortage_suggestions`:

```python
        demand = {}
        for r in conn.execute(
            "SELECT wl.part_id, w.location_id, SUM(wl.qty_required) AS q FROM work_order_lines wl "
            "JOIN work_orders w ON w.id = wl.work_order_id WHERE w.status = 'allocated' "
            "GROUP BY wl.part_id, w.location_id"):
            demand[(r["part_id"], r["location_id"] or stock.default_shelf(conn))] = r["q"]
        if not demand:
            return []
        on_order = {}
        for r in conn.execute(
            "SELECT pl.part_id, p.location_id, SUM(pl.qty - pl.qty_received) AS q FROM purchase_order_lines pl "
            "JOIN purchase_orders p ON p.id = pl.po_id WHERE p.status = 'ordered' GROUP BY pl.part_id, p.location_id"):
            on_order[(r["part_id"], r["location_id"] or stock.default_shelf(conn))] = r["q"]
        out = []
        for (part_id, loc_id), required in demand.items():
            part = ...  # as today
            free = stock.free_on_shelf(conn, part_id, loc_id)
            incoming = on_order.get((part_id, loc_id), 0) or 0
            short = required - free - incoming
            ...
            loc = conn.execute("SELECT name, kind FROM stock_locations WHERE id = ?", (loc_id,)).fetchone()
            out.append({..., "location_id": loc_id, "location_name": loc["name"], "location_kind": loc["kind"]})
    out.sort(key=lambda r: (r["location_kind"] != "ours", r["location_name"], r["supplier_name"] or "~", r["part_no"]))
```

`shortage_suggestions_grouped`:

```python
    groups: dict = {}
    for s in shortage_suggestions(db):
        g = groups.setdefault(s["location_id"], {"location_id": s["location_id"], "location_name": s["location_name"],
                                                 "location_kind": s["location_kind"], "suppliers": {}})
        sg = g["suppliers"].setdefault(s["supplier_id"], {"supplier_id": s["supplier_id"],
                                                          "supplier_name": s["supplier_name"], "lines": []})
        sg["lines"].append(s)
    return [{**g, "suppliers": list(g["suppliers"].values())} for g in groups.values()]
```

`create_pos_from_suggestions(db, selections: dict[tuple[int, int], float], user=None)`: key `by_supplier` on `(supplier_id, location_id)`; INSERT includes `location_id`.

Router `suggestions_apply`:

```python
    selections = {}
    for v in form.getlist("buy"):
        pid, _, lid = str(v).partition(":")
        if pid.isdigit() and lid.isdigit():
            qty = _num(form.get(f"qty_{pid}_{lid}"))
            if qty and qty > 0:
                selections[(int(pid), int(lid))] = qty
```

Router `new_form`/`create`: context `"shelves": catalog_repo.pickable_shelves(db)`, `"shelf_selected": catalog_repo.default_shelf_id(db)`; `data["location_id"] = _int(form.get("location_id"))`.

- [ ] **Step 5: Templates**

`purchase_order_form.html` after the supplier field:

```jinja
        <label for="location_id">Receive into shelf</label>
        {% with shelf_selected=values.get('location_id') or shelf_selected, shelf_field='location_id' %}{% include "_shelf_select.html" %}{% endwith %}
```

`purchase_order_detail.html` header `.note`: append `· Receive into <b>{{ po.location_name }}</b>{% if po.location_kind == 'customer' %} <span class="badge warn">customer shelf</span>{% endif %}`.

`purchase_order_suggestions.html`: rewrite the table body as shelf group → supplier group → lines; checkbox `value="{{ s.part_id }}:{{ s.location_id }}"`, qty input `name="qty_{{ s.part_id }}_{{ s.location_id }}"`, `data-sup="{{ g.location_id }}-{{ sg.supplier_id }}"`; a shelf header row `<tr class="suprow"><td></td><td colspan="8"><span class="supname">{{ g.location_name }}</span> {% if g.location_kind == 'customer' %}<span class="badge warn">customer shelf</span>{% else %}<span class="badge stock">ours</span>{% endif %}</td></tr>`; the `orderable` set becomes `groups | map(attribute='suppliers') | sum(start=[]) | selectattr('supplier_id') | list`; the intro note says "grouped by shelf, then supplier. One draft PO is created per supplier per shelf."

`export.py`: in `po_pdf` meta list add `("Deliver to shelf", po.get("location_name") or "")`; in `po_csv` nothing structural changes (shelf is a header property) — add a first comment-free line? No: CSVs are uploaded to distributors; leave the CSV unchanged (ruling: spec said "a shelf column in the CSV" — the CSV is a distributor cart format, a stray column breaks uploads; record in ledger).

- [ ] **Step 6: Run** `uv run pytest tests/test_shelves_purchasing.py tests/test_purchase_orders.py -q` → PASS; update existing PO tests that index `groups[i]["lines"]` to `groups[i]["suppliers"][j]["lines"]` and `create_pos_from_suggestions` keys to `(part_id, main)`; full suite green.
- [ ] **Step 7: Commit** `git add -A && git commit -m "Purchase orders: receive into a shelf; shortages and suggested POs per shelf"`

---

### Task 6: Work orders draw strictly from one shelf

**Files:**
- Modify: `work_orders/repo.py` (`create_work_order`, `get_work_order`, `order_header`, `create_work_orders_for_order`), `router.py` (`_render_form`, `create`, `build_from_order`, `build_from_order_apply`), templates `work_order_form.html`, `work_order_from_order.html`, `work_order_detail.html`
- Test: `tests/test_shelves_work_orders.py`

**Interfaces:**
- Produces: `create_work_order(db, data)` always stores a shelf (`data.get("location_id") or default`); `get_work_order` returns `shelf` `{id, name, kind, owner_name}`, `customer_supplied: bool`, per line `available`/`short` from the WO shelf, `loaded_build_cost_charged`, `loaded_build_cost_reference`; `order_header` includes `customer_id`; `create_work_orders_for_order(db, order_id, selections, user=None, *, location_id=None)`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_shelves_work_orders.py
import pytest

from partpilot.web.core import FeatureRegistry
from partpilot.web.core.db import Database
from partpilot.web.features.assemblies import feature as assemblies_feature
from partpilot.web.features.assemblies import repo as asmrepo
from partpilot.web.features.catalog import feature as catalog_feature
from partpilot.web.features.catalog import repo as catrepo
from partpilot.web.features.catalog import stock
from partpilot.web.features.contacts import feature as contacts_feature
from partpilot.web.features.contacts import repo as conrepo
from partpilot.web.features.customer_orders import feature as co_feature
from partpilot.web.features.customer_orders import repo as corepo
from partpilot.web.features.work_orders import feature as wo_feature
from partpilot.web.features.work_orders import repo


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "wo.db")
    reg = FeatureRegistry()
    reg.register(catalog_feature, assemblies_feature, contacts_feature, co_feature, wo_feature)
    database.apply_migrations(reg)
    return database


def _world(db):
    r1 = catrepo.create_part(db, part={"part_no": "R-1"},
                             supplier_lines=[{"supplier_name": "X", "unit_price": 1, "reel_qty": 1, "is_default": True}],
                             opening={"qty": 3000})
    asm = asmrepo.create_assembly(db, {"part_no": "PROD"})
    asmrepo.add_bom_line(db, asm, r1, 1, None)
    cust = conrepo.create_contact(db, {"kind": "customer", "name": "Acme"})
    shelf = catrepo.create_shelf(db, kind="customer", owner_contact_id=cust)
    return r1, asm, cust, shelf


def test_customer_wo_is_short_even_though_main_is_full(db):
    r1, asm, cust, shelf = _world(db)
    wo = repo.get_work_order(db, repo.create_work_order(db, {"assembly_id": asm, "qty": 100, "location_id": shelf}))
    ln = wo["lines"][0]
    assert ln["available"] == 0 and ln["short"] == 100 and wo["short_count"] == 1
    assert wo["shelf"]["name"] == "Acme" and wo["customer_supplied"] is True
    assert wo["loaded_build_cost_charged"] == 0 and wo["loaded_build_cost_reference"] == pytest.approx(130.0)


def test_issue_and_finish_stay_on_the_wo_shelf(db):
    r1, asm, cust, shelf = _world(db)
    with db.connect() as conn:
        stock.post_movement(conn, r1, delta=100, mtype=stock.RECEIVE, location_id=shelf); conn.commit()
    wo = repo.create_work_order(db, {"assembly_id": asm, "qty": 100, "location_id": shelf})
    repo.issue_work_order(db, wo)
    repo.finish_work_order(db, wo)
    with db.connect() as conn:
        main = stock.default_shelf(conn)
        assert stock.shelf_on_hand(conn, r1, shelf) == 0 and stock.shelf_on_hand(conn, r1, main) == 3000
        assert stock.shelf_on_hand(conn, asm, shelf) == 100 and stock.shelf_on_hand(conn, asm, main) == 0


def test_work_order_without_shelf_uses_default(db):
    r1, asm, cust, shelf = _world(db)
    wo = repo.get_work_order(db, repo.create_work_order(db, {"assembly_id": asm, "qty": 10}))
    assert wo["shelf"]["kind"] == "ours" and wo["customer_supplied"] is False
    assert wo["lines"][0]["available"] == 3000 and wo["loaded_build_cost_charged"] == pytest.approx(13.0)


def test_from_order_uses_preferred_shelf(db):
    r1, asm, cust, shelf = _world(db)
    order = corepo.create_order(db, {"customer_id": cust})
    corepo.add_line(db, order, asm, 5, 10.0, None)
    line_id = corepo.get_order(db, order)["lines"][0]["id"]
    assert repo.order_header(db, order)["customer_id"] == cust
    ids = repo.create_work_orders_for_order(db, order, {line_id: 5},
                                            location_id=catrepo.preferred_shelf_id(db, customer_id=cust, order_id=order))
    assert repo.get_work_order(db, ids[0])["location_id"] == shelf
```

- [ ] **Step 2: Run** → FAIL (`KeyError: 'shelf'`).
- [ ] **Step 3: Repo**

`create_work_order`: `loc = data.get("location_id") or stock.default_shelf(conn)`; validate `conn.execute("SELECT 1 FROM stock_locations WHERE id = ? AND retired_at IS NULL", (loc,))` else `ValueError("Choose an active shelf.")`; store `loc`.

`get_work_order` head SELECT adds `l.name AS shelf_name, l.kind AS shelf_kind, l.owner_contact_id AS shelf_owner_id` via `LEFT JOIN stock_locations l ON l.id = w.location_id`; lines SELECT adds
`COALESCE(pk.on_hand, 0) AS shelf_on_hand, COALESCE(pk.allocated, 0) AS shelf_allocated` via
`LEFT JOIN part_stock pk ON pk.part_id = p.id AND pk.location_id = COALESCE(w.location_id, ?)` (join `work_orders w` and bind the default shelf). Then:

```python
    shelf_id = head["location_id"] or default
    wo["shelf"] = {"id": shelf_id, "name": head["shelf_name"] or "Main", "kind": head["shelf_kind"] or "ours",
                   "owner_name": owner_name}   # owner_name via contacts lookup when shelf_owner_id
    wo["customer_supplied"] = wo["shelf"]["kind"] == "customer"
    ...
            d["available"] = d["shelf_on_hand"] - d["shelf_allocated"]
    ...
    wo["loaded_build_cost_reference"] = loaded_build_cost
    wo["loaded_build_cost_charged"] = 0.0 if wo["customer_supplied"] else loaded_build_cost
    wo["loaded_build_cost"] = wo["loaded_build_cost_charged"]
```

`order_header`: add `o.customer_id`. `create_work_orders_for_order(..., *, location_id=None)`: pass `"location_id": location_id` into each `create_work_order`.

- [ ] **Step 4: Router + templates**

`_render_form`: `"shelves": catalog_repo.pickable_shelves(db), "shelf_selected": values.get("location_id") or catalog_repo.default_shelf_id(db)`; `work_order_form.html` location block → `{% include "_shelf_select.html" %}` with label "Shelf (stock is issued from here)".

`build_from_order`: context adds `"shelves": catalog_repo.pickable_shelves(db, customer_id=order["customer_id"])`, `"shelf_selected": catalog_repo.preferred_shelf_id(db, customer_id=order["customer_id"], order_id=order_id)`, `"customer_has_shelf": bool(catalog_repo.shelves_for_customer(db, order["customer_id"])) if order["customer_id"] else True`. `work_order_from_order.html`: above the submit row

```jinja
    <div style="margin-top:1rem;">
      <label for="location_id">Build from shelf</label>
      {% with shelf_field='location_id' %}{% include "_shelf_select.html" %}{% endwith %}
      {% if not customer_has_shelf and order.customer_id %}
      <p class="note">This customer has no shelf yet — the build will draw from our stock.
        <form method="post" action="/catalog/shelves" style="display:inline;">
          <input type="hidden" name="kind" value="customer"><input type="hidden" name="owner_contact_id" value="{{ order.customer_id }}">
          <input type="hidden" name="return_to" value="/work-orders/from-order/{{ order.id }}">
          <button type="submit" class="ghost">Create customer shelf</button></form></p>
      {% endif %}
    </div>
```
`build_from_order_apply`: `location_id=_int(form.get("location_id"))` passed through.

`work_order_detail.html`: header note gets `Shelf: <b>{{ w.shelf.name }}</b>{% if w.customer_supplied %} <span class="badge warn">customer-owned parts</span>{% endif %}`; the cost card shows, when `w.customer_supplied`, `<div class="card"><b>0.00</b><small>charged — material customer-supplied (ref. {{ "%.2f"|format(w.loaded_build_cost_reference) }})</small></div>`; the "Available" column header becomes "Available on shelf".

- [ ] **Step 5: Run** `uv run pytest tests/test_shelves_work_orders.py tests/test_work_orders.py tests/test_planning.py -q` → PASS; full suite green.
- [ ] **Step 6: Commit** `git add -A && git commit -m "Work orders: strict single-shelf availability, issue and finish; customer-supplied material"`

---

### Task 7: Allocation, fulfilment proposals and despatch are shelf-aware

**Files:**
- Modify: `customer_orders/migrations.py` (v7), `customer_orders/repo.py` (`allocate_order`), `work_orders/repo.py` (`fulfilment_proposals`), `despatch/repo.py` (`shippable_lines`, `dispatch`), despatch templates (shelf line when customer shelf)
- Test: `tests/test_shelves_fulfilment.py`

- [ ] **Step 1: Failing tests**

```python
# tests/test_shelves_fulfilment.py
import pytest

from partpilot.web.core import FeatureRegistry
from partpilot.web.core.db import Database
from partpilot.web.features.assemblies import feature as assemblies_feature
from partpilot.web.features.assemblies import repo as asmrepo
from partpilot.web.features.catalog import feature as catalog_feature
from partpilot.web.features.catalog import repo as catrepo
from partpilot.web.features.catalog import stock
from partpilot.web.features.contacts import feature as contacts_feature
from partpilot.web.features.contacts import repo as conrepo
from partpilot.web.features.customer_orders import feature as co_feature
from partpilot.web.features.customer_orders import repo as corepo
from partpilot.web.features.despatch import feature as despatch_feature
from partpilot.web.features.despatch import repo as drepo
from partpilot.web.features.work_orders import feature as wo_feature
from partpilot.web.features.work_orders import repo as worepo


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "f.db")
    reg = FeatureRegistry()
    reg.register(catalog_feature, assemblies_feature, contacts_feature, co_feature, wo_feature, despatch_feature)
    database.apply_migrations(reg)
    return database


def _world(db):
    prod = asmrepo.create_assembly(db, {"part_no": "PROD"})
    acme = conrepo.create_contact(db, {"kind": "customer", "name": "Acme"})
    beta = conrepo.create_contact(db, {"kind": "customer", "name": "Beta"})
    a_shelf = catrepo.create_shelf(db, kind="customer", owner_contact_id=acme)
    b_shelf = catrepo.create_shelf(db, kind="customer", owner_contact_id=beta)
    main = catrepo.default_shelf_id(db)
    with db.connect() as conn:
        stock.post_movement(conn, prod, delta=10, mtype=stock.BUILD, location_id=a_shelf)
        stock.post_movement(conn, prod, delta=50, mtype=stock.BUILD, location_id=b_shelf)
        stock.post_movement(conn, prod, delta=3, mtype=stock.BUILD, location_id=main)
        conn.commit()
    return prod, acme, beta, a_shelf, b_shelf, main


def test_proposals_count_only_customer_and_own_shelves(db):
    prod, acme, beta, a_shelf, b_shelf, main = _world(db)
    order = corepo.create_order(db, {"customer_id": acme})
    corepo.add_line(db, order, prod, 20, 10.0, None)
    p = worepo.fulfilment_proposals(db, order)[0]
    assert p["free"] == 13 and p["shortfall"] == 7      # Acme 10 + Main 3; Beta's 50 ignored


def test_allocation_prefers_customer_shelf_then_main_and_despatch_issues_from_them(db):
    prod, acme, beta, a_shelf, b_shelf, main = _world(db)
    order = corepo.create_order(db, {"customer_id": acme})
    corepo.add_line(db, order, prod, 12, 10.0, None)
    assert corepo.allocate_order(db, order) == 12
    with db.connect() as conn:
        allocs = {r["location_id"]: r["qty"] for r in conn.execute("SELECT location_id, qty FROM allocations")}
        assert allocs == {a_shelf: 10, main: 2}
        assert stock.free_on_shelf(conn, prod, a_shelf) == 0 and stock.free_on_shelf(conn, prod, main) == 1
        assert conn.execute("SELECT total_alloc FROM parts WHERE id = ?", (prod,)).fetchone()[0] == 2
    line_id = corepo.get_order(db, order)["lines"][0]["id"]
    d = drepo.create_packing_list(db, order, {line_id: 12})
    drepo.set_packing(db, d, {ln["id"] for ln in drepo.get_despatch(db, d)["lines"]})
    drepo.confirm_packed(db, d)
    drepo.dispatch(db, d)
    with db.connect() as conn:
        assert stock.shelf_on_hand(conn, prod, a_shelf) == 0
        assert stock.shelf_on_hand(conn, prod, main) == 1
        assert stock.shelf_on_hand(conn, prod, b_shelf) == 50


def test_allocate_without_customer_shelf_uses_ours(db):
    prod, acme, beta, a_shelf, b_shelf, main = _world(db)
    carl = conrepo.create_contact(db, {"kind": "customer", "name": "Carl"})
    order = corepo.create_order(db, {"customer_id": carl})
    corepo.add_line(db, order, prod, 2, 10.0, None)
    assert corepo.allocate_order(db, order) == 2
    with db.connect() as conn:
        assert {r[0] for r in conn.execute("SELECT location_id FROM allocations")} == {main}
```

(`create_packing_list`/`set_packing`/`confirm_packed` signatures: check `despatch/repo.py:156-250` and adapt the test calls to the real argument names before running.)

- [ ] **Step 2: Run** → FAIL (`allocations.location_id` missing).
- [ ] **Step 3: Migration 7** (`customer_orders/migrations.py`):

```python
    Migration(
        version=7,
        name="allocation shelf",
        sql="""
        -- Which shelf an allocation reserves from. NULL on legacy rows = our default shelf.
        ALTER TABLE allocations ADD COLUMN location_id INTEGER REFERENCES stock_locations(id);
        """,
    ),
```

- [ ] **Step 4: Repo**

`customer_orders.repo.allocate_order`:

```python
    from ..catalog import repo as catrepo
    newly = 0.0
    with db.connect() as conn:
        head = conn.execute("SELECT customer_id FROM customer_orders WHERE id = ?", (order_id,)).fetchone()
    shelves = catrepo.candidate_shelf_ids(db, customer_id=head["customer_id"] if head else None, order_id=order_id)
    with db.connect() as conn:
        for ln in lines:
            ...
            need = ...
            for loc in shelves:
                if need <= 0:
                    break
                take = min(need, stock.free_on_shelf(conn, ln["part_id"], loc))
                if take > 0:
                    conn.execute("INSERT INTO allocations (customer_order_line_id, part_id, qty, location_id) VALUES (?, ?, ?, ?)",
                                 (ln["id"], ln["part_id"], take, loc))
                    stock.recompute_alloc(conn, ln["part_id"])
                    need -= take; newly += take
        conn.commit()
    return newly
```

`work_orders.repo.fulfilment_proposals`: compute `shelves = catrepo.candidate_shelf_ids(db, customer_id=..., order_id=order_id)` once; `free = sum(stock.free_on_shelf(conn, d["part_id"], s) for s in shelves)`.

`despatch.repo.shippable_lines`: `on_hand = sum(stock.shelf_on_hand(conn, part_id, s) for s in shelves)` (same candidate list; order's `customer_id` from `customer_orders`).

`despatch.repo.dispatch`: for each despatch line, consume allocations **per shelf** and post ISSUE per shelf:

```python
            if ln["part_id"]:
                remaining = ln["qty"]
                if ln["order_line_id"]:
                    for a in conn.execute("SELECT id, location_id, qty FROM allocations WHERE customer_order_line_id = ? "
                                          "AND part_id = ? ORDER BY id", (ln["order_line_id"], ln["part_id"])).fetchall():
                        if remaining <= 0: break
                        take = min(a["qty"], remaining)
                        loc = a["location_id"] or stock.default_shelf(conn)
                        stock.post_movement(conn, ln["part_id"], delta=-take, mtype=stock.ISSUE, reference=desp_ref,
                                            note="despatch", user=user, location_id=loc)
                        if take >= a["qty"]:
                            conn.execute("DELETE FROM allocations WHERE id = ?", (a["id"],))
                        else:
                            conn.execute("UPDATE allocations SET qty = qty - ? WHERE id = ?", (take, a["id"]))
                        remaining -= take
                    stock.recompute_alloc(conn, ln["part_id"])
                if remaining > 0:   # unallocated remainder ships from our default shelf
                    stock.post_movement(conn, ln["part_id"], delta=-remaining, mtype=stock.ISSUE, reference=desp_ref,
                                        note="despatch", user=user, location_id=stock.default_shelf(conn))
```
Delete `_consume_allocation` (now inlined). Despatch detail/packing templates: where the order's customer has a shelf involved, show `<span class="badge warn">ships from customer shelf</span>` next to lines whose allocations are on a customer shelf (`get_despatch` lines gain `customer_shelf_qty` via a subquery on allocations joined to stock_locations kind='customer').

- [ ] **Step 5: Run** `uv run pytest tests/test_shelves_fulfilment.py tests/test_customer_orders.py tests/test_despatch.py -q` → PASS; full suite green.
- [ ] **Step 6: Commit** `git add -A && git commit -m "Allocation, fulfilment and despatch are shelf-aware"`

---

### Task 8: Resolver stock index per shelf; shelf selector on all three upload pages

**Files:**
- Modify: `catalog/stock_index.py`, `purchasing/service.py` (`resolve_bom_file`, `run_purchase`, `ResolvedRun.stock_shelf`, `PurchaseResult.stock_shelf`), `purchasing/router.py` + `upload.html` + `result.html`, `setup/router.py` + `import_parts_upload.html`/`_map`/`_review`/`_result`, `assemblies/router.py` + `assembly_import.html`/`assembly_import_review.html`, `catalog/import_parts.py` (`build_import_plan(db, run, stock_location_id=None)` → `shelf_free`)
- Test: `tests/test_shelves_resolver.py`; update `tests/test_setup_import_parts_web.py` (remove the two `ignore_stock` tests, add the selector tests), `tests/test_purchasing.py` if it posts `check_stock`.

**Interfaces:**
- Produces: `build_stock_index(db, location_ids: list[int]) -> StockIndex`; `resolve_bom_file(..., check_stock=True, stock_location_id: int | None = None, ...)`; `ResolvedRun.stock_shelf: str | None`; form field `stock_shelf` (`"none"` or shelf id) on all three pages; `build_import_plan(db, run, stock_location_id=None)` adds `shelf_free` to in-inventory rows.

- [ ] **Step 1: Failing tests**

```python
# tests/test_shelves_resolver.py
import pytest
from fastapi.testclient import TestClient

import partpilot.web.app as web_app
import partpilot.web.features.setup.router as setup_router
import partpilot.web.features.purchasing.router as purch_router
import partpilot.web.features.assemblies.router as asm_router
from partpilot.web.features.catalog import repo as catrepo
from partpilot.web.features.catalog import stock
from partpilot.web.features.catalog.stock_index import build_stock_index
from partpilot.web.features.contacts import repo as conrepo
from partpilot.web.features.assemblies import repo as asmrepo


@pytest.fixture
def app(tmp_path):
    a = web_app.create_app(db_path=tmp_path / "p.db", data_dir=tmp_path / "data", secret_key="s")
    a.state.store.create_user("boss", "pw", role="admin")
    return a


def _c(app):
    c = TestClient(app); c.post("/login", data={"username": "boss", "password": "pw"}); return c


def _world(db):
    cust = conrepo.create_contact(db, {"kind": "customer", "name": "Acme"})
    shelf = catrepo.create_shelf(db, kind="customer", owner_contact_id=cust)
    pid = catrepo.create_part(db, part={"part_no": "RC0603FR-071KL", "mfr_pno": "RC0603FR-071KL",
                                        "category": "RESISTOR", "value": "1K/1%/0.1W/0603"},
                              supplier_lines=[], opening={"qty": 3000})
    return pid, shelf


def test_stock_index_is_built_from_chosen_shelves_only(app):
    db = app.state.database
    pid, shelf = _world(db)
    main = catrepo.default_shelf_id(db)
    assert build_stock_index(db, [main]).match_mpn("RC0603FR-071KL").free == 3000
    assert build_stock_index(db, [shelf]).match_mpn("RC0603FR-071KL") is None


CSV = "Designator,Value,Qty\nR1,RC0603FR-071KL,1\n"


def test_upload_pages_offer_shelf_selector_and_pass_it(app, monkeypatch):
    db = app.state.database
    pid, shelf = _world(db)
    seen = {}

    def spy(bom_path, **kw):
        seen.update(kw)
        from partpilot.bom.normalize import load_bom
        from partpilot.models import LineKind, ResolvedLine, Status
        from partpilot.web.features.purchasing.service import ResolvedRun
        lines = load_bom(bom_path, kw.get("mappings"))
        return ResolvedRun(resolved=[ResolvedLine(line=ln, kind=LineKind.MPN, status=Status.NOT_FOUND) for ln in lines],
                           build_qty=1, currency="SEK", stock_checked=kw.get("check_stock", True),
                           mouser_enabled=False, warnings=[], stock_shelf="Acme")
    monkeypatch.setattr(setup_router, "resolve_bom_file", spy)
    c = _c(app)
    html = c.get("/setup/import-parts").text
    assert 'name="stock_shelf"' in html and "Customer: Acme" in html and "Don't check stock" in html
    assert "ignore_stock" not in html
    r = c.post("/setup/import-parts", files={"file": ("b.csv", CSV, "text/csv")}, data={"stock_shelf": str(shelf)})
    job = r.text.split('name="job_id" value="')[1].split('"')[0]
    r = c.post("/setup/import-parts/resolve", data={"job_id": job, "map_0": "refdes", "map_1": "device", "map_2": "qty"})
    assert seen["stock_location_id"] == shelf and seen["check_stock"] is True
    assert "Stock checked on <b>Acme</b>" in r.text
    assert "in catalog" in r.text and "0 free on Acme" in r.text   # linked row shows shelf_free


def test_upload_none_skips_stock(app, monkeypatch):
    db = app.state.database
    _world(db)
    seen = {}
    def spy(bom_path, **kw):
        seen.update(kw)
        from partpilot.web.features.purchasing.service import ResolvedRun
        return ResolvedRun(resolved=[], build_qty=1, currency="SEK", stock_checked=False, mouser_enabled=False,
                           warnings=[], stock_shelf=None)
    monkeypatch.setattr(setup_router, "resolve_bom_file", spy)
    c = _c(app)
    r = c.post("/setup/import-parts", files={"file": ("b.csv", CSV, "text/csv")}, data={"stock_shelf": "none"})
    job = r.text.split('name="job_id" value="')[1].split('"')[0]
    c.post("/setup/import-parts/resolve", data={"job_id": job, "map_0": "refdes", "map_1": "device", "map_2": "qty"})
    assert seen["check_stock"] is False


def test_upload_rejects_retired_shelf(app):
    db = app.state.database
    pid, shelf = _world(db)
    with db.connect() as conn:
        conn.execute("UPDATE stock_locations SET retired_at = datetime('now') WHERE id = ?", (shelf,)); conn.commit()
    r = _c(app).post("/setup/import-parts", files={"file": ("b.csv", CSV, "text/csv")}, data={"stock_shelf": str(shelf)})
    assert r.status_code == 400


def test_purchasing_and_assembly_pages_have_selector(app):
    db = app.state.database
    _world(db)
    asm = asmrepo.create_assembly(db, {"part_no": "PROD"})
    c = _c(app)
    assert 'name="stock_shelf"' in c.get("/purchasing").text and "check_stock" not in c.get("/purchasing").text
    assert 'name="stock_shelf"' in c.get(f"/assemblies/{asm}/import").text
```

- [ ] **Step 2: Run** → FAIL (`build_stock_index() takes 1 positional argument`).
- [ ] **Step 3: Implement**

`stock_index.py`:

```python
def build_stock_index(db: Database, location_ids: list[int]) -> StockIndex:
    """Index released, non-document parts with their on-hand/allocated summed over ``location_ids``."""
    if not location_ids:
        return StockIndex.build([])
    marks = ",".join("?" for _ in location_ids)
    with db.connect() as conn:
        rows = conn.execute(
            f"""SELECT p.id, p.part_no, COALESCE(p.value,'') AS value, COALESCE(p.description,'') AS description,
                       COALESCE(p.category,'') AS category, COALESCE(p.mfr_pno,'') AS mfr_pno,
                       COALESCE((SELECT SUM(pk.on_hand) FROM part_stock pk WHERE pk.part_id = p.id AND pk.location_id IN ({marks})), 0) AS on_hand,
                       COALESCE((SELECT SUM(pk.allocated) FROM part_stock pk WHERE pk.part_id = p.id AND pk.location_id IN ({marks})), 0) AS allocated
                FROM parts p WHERE COALESCE(p.is_document, 0) = 0 AND p.version_status = 'released'""",
            [*location_ids, *location_ids]).fetchall()
    # ... build StockItem(on_hand=r["on_hand"], allocated=r["allocated"], ...) as today
```

`service.py`: `ResolvedRun` gains `stock_shelf: str | None = None`; `PurchaseResult` likewise. `resolve_bom_file(..., stock_location_id: int | None = None)`:

```python
    stock = None; stock_shelf = None
    if check_stock and database is not None:
        from partpilot.web.features.catalog import repo as catalog_repo
        from partpilot.web.features.catalog.stock_index import build_stock_index
        if stock_location_id:
            shelf = catalog_repo.get_location(database, stock_location_id)
            if shelf is None or shelf["retired_at"]:
                raise ValueError("That shelf is retired or unknown.")
            ids, stock_shelf = [stock_location_id], shelf["name"]
        else:
            ids, stock_shelf = [s["id"] for s in catalog_repo.locations(database, kind="ours")], "our shelves"
        stock = build_stock_index(database, ids)
    ...
    resolved = resolve_bom(...)
    if stock_shelf:
        for ln in resolved:
            if ln.status == Status.IN_STOCK and ln.flag_reason and ln.flag_reason.startswith("in stock:"):
                ln.flag_reason = ln.flag_reason.replace("in stock:", f"in stock on {stock_shelf}:", 1)
    return ResolvedRun(..., stock_shelf=stock_shelf)
```
`run_purchase(..., stock_location_id=None)` passes through and copies `stock_shelf`.

Shared router helper (put in `catalog/repo.py` as it is pure data): 

```python
def parse_stock_shelf(db: Database, raw: str | None) -> tuple[bool, int | None]:
    """Form value of the shelf selector -> (check_stock, stock_location_id). Raises ValueError."""
    v = (raw or "").strip()
    if v == "none":
        return False, None
    if not v:
        return True, default_shelf_id(db)
    if not v.isdigit():
        raise ValueError("Choose a shelf.")
    shelf = get_location(db, int(v))
    if shelf is None or shelf["retired_at"]:
        raise ValueError("That shelf is retired or unknown.")
    return True, int(v)
```

Routers: purchasing `run` replaces `check_stock: bool = Form(False)` with `stock_shelf: str = Form("")` → `check_stock, loc = catalog_repo.parse_stock_shelf(db, stock_shelf)` (ValueError → 400 `error.html`), passes `check_stock=check_stock, stock_location_id=loc` to `run_purchase`; GET passes `"shelves": catalog_repo.pickable_shelves(db) + catalog_repo.locations(db, kind="customer")` — simpler: `"shelves": catalog_repo.locations(db)` (active, ours first), `"shelf_selected": catalog_repo.default_shelf_id(db)`. Setup upload: store `check_stock` and `stock_location_id` in meta (parse at upload → 400 on error), pass both to `resolve_bom_file`, record `meta["stock_shelf"] = run.stock_shelf`, template contexts get `stock_shelf`. Assembly import: same two fields; `import_resolve` reads `form.get("stock_shelf")`.

Templates: each upload page

```jinja
    <label for="stock_shelf">Check stock on shelf</label>
    {% with shelf_field='stock_shelf', shelf_none=True %}{% include "_shelf_select.html" %}{% endwith %}
    <p class="note">Rows already covered by free stock on that shelf are marked in stock and skip the distributors.
      Pick a customer's shelf when buying for them; pick "Don't check stock" to price every row.</p>
```
and the review/result pages print `{% if stock_shelf %}<p class="note">Stock checked on <b>{{ stock_shelf }}</b>.</p>{% else %}<p class="note">Stock not checked — every row went to the distributors.</p>{% endif %}` in place of the old "stock checked"/"own stock ignored" notes. Remove `ignore_stock`/`check_stock` inputs and the `check_stock` meta key.

`catalog/import_parts.py` `build_import_plan(db, run, stock_location_id=None)`: for `IN_INVENTORY` rows add `"shelf_free": stock.free_on_shelf(conn, existing_id, stock_location_id) if stock_location_id else None`; review templates show `{% if it.shelf_free is not none %}<span class="flag">{{ "%g"|format(it.shelf_free) }} free on {{ stock_shelf }}</span>{% endif %}` and the status badge text becomes "in catalog".

- [ ] **Step 4: Update old tests** — in `tests/test_setup_import_parts_web.py` delete `test_ignore_stock_checkbox_skips_own_stock_check` and `test_own_stock_is_checked_by_default`; `_fake_resolve` adds `stock_shelf="Main"`. In `tests/test_purchasing.py`/`tests/test_web.py` replace any `check_stock` form data with `stock_shelf`.
- [ ] **Step 5: Run** `uv run pytest tests/test_shelves_resolver.py tests/test_setup_import_parts_web.py tests/test_purchasing.py tests/test_web.py tests/test_assemblies.py -q` → PASS; full suite green.
- [ ] **Step 6: Commit** `git add -A && git commit -m "Resolver: stock index per shelf; shelf selector replaces stock checkboxes on all upload pages"`

---

### Task 9: Reports shelf filter, docs, final checks

**Files:**
- Modify: `reports/repo.py` (`_filter`, `stock_movements`, `stock_movement_summary` gain `location_id`), `reports/router.py`, `templates/stock_movements.html` (shelf column + filter select)
- Modify: `README.md`, `docs/design-rules.md` (change log), spec status line
- Test: `tests/test_shelves_reports.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_shelves_reports.py
import pytest
from fastapi.testclient import TestClient

import partpilot.web.app as web_app
from partpilot.web.features.catalog import repo as catrepo
from partpilot.web.features.catalog import stock
from partpilot.web.features.contacts import repo as conrepo
from partpilot.web.features.reports import repo as rrepo


@pytest.fixture
def app(tmp_path):
    a = web_app.create_app(db_path=tmp_path / "p.db", data_dir=tmp_path / "data", secret_key="s")
    a.state.store.create_user("boss", "pw", role="admin")
    return a


def test_movement_report_filters_by_shelf(app):
    db = app.state.database
    cust = conrepo.create_contact(db, {"kind": "customer", "name": "Acme"})
    shelf = catrepo.create_shelf(db, kind="customer", owner_contact_id=cust)
    pid = catrepo.create_part(db, part={"part_no": "R-1"}, supplier_lines=[], opening={"qty": 5})
    with db.connect() as conn:
        stock.post_movement(conn, pid, delta=7, mtype=stock.RECEIVE, location_id=shelf); conn.commit()
    assert len(rrepo.stock_movements(db)) == 2
    rows = rrepo.stock_movements(db, location_id=shelf)
    assert len(rows) == 1 and rows[0]["location_name"] == "Acme"
    assert rrepo.stock_movement_summary(db, location_id=shelf)["total_in"] == 7
    c = TestClient(app); c.post("/login", data={"username": "boss", "password": "pw"})
    html = c.get(f"/reports/stock-movements?location_id={shelf}").text
    assert 'name="location_id"' in html and "Acme" in html
```

- [ ] **Step 2: Run** → FAIL (`unexpected keyword location_id`).
- [ ] **Step 3: Implement** — `_filter(start, end, mtype, location_id=None)` appends `m.location_id = ?`; both public functions take `location_id=None`; router reads `location_id: int | None = None`, passes `"shelves": catalog_repo.locations(db, include_retired=True)` and `"location_id"`; template adds a `<select name="location_id">` (All shelves + each) in the filter form and a "Shelf" column already present as `location_name` — ensure it renders with a kind badge.
- [ ] **Step 4: Docs** — README: under Parts add "Stock shelves" paragraph (ours vs customer-owned, created from contact / order, totals exclude customer shelves); Purchase Orders: "receive-into shelf; suggestions per shelf"; Work Orders: "strict single shelf; customer-supplied material"; Purchasing/Setup import: "Check stock on shelf" selector replaces the checkboxes. `docs/design-rules.md` change log 2026-10-16: shelf badges (`.badge stock` = ours, `.badge warn` = customer), shared `_shelf_select.html` picker, shelf header rows in suggestions. Spec: status → "implemented 2026-10-16".
- [ ] **Step 5: Run** `uv run pytest -q` → green.
- [ ] **Step 6: Commit** `git add -A && git commit -m "Reports: shelf filter; docs for customer-owned stock"`

---

## Self-review notes

- Spec coverage: data model (T1, T5, T7), stock core (T1), shelf repo (T2), pages/contact/order/Setup (T3), part page (T4), purchasing (T5), work orders (T6), fulfilment/allocation/despatch (T7), resolver + three upload screens + `shelf_free` (T8), reports + docs (T9). Shelf value line: T2/T3. CSV shelf column: ruled out in T5 (distributor cart format) — ledger the ruling.
- Type consistency: `candidate_shelf_ids` / `preferred_shelf_id` / `pickable_shelves` / `parse_stock_shelf` / `default_shelf_id` all in `catalog.repo`; `default_shelf`, `free_on_shelf`, `shelf_on_hand`, `recompute_alloc`, `post_movement(location_id=...)` in `catalog.stock`.
- Review Focus items each have a named test in T4, T5, T6, T7, T8.
