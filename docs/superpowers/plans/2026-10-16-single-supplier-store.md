# Single Supplier Store Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Drop the catalog `suppliers` table so Contacts (kind `supplier`) is the only supplier record, re-pointing `part_suppliers`, `purchase_orders` and `goods_receipts` at `contacts(id)`, with the migration runner backing the database up first.

**Architecture:** Two Python-callable migrations (catalog v23, purchase-orders v6) rebuild the three FK tables through a shared `core.db.rebuild_table` helper, mapping old supplier ids to contact ids via a temporary `suppliers.contact_id` column. The runner gains callable support and a pre-migration online backup. Every repo that joined `suppliers` joins `contacts`; the mirror-on-save is deleted; a kind-change guard protects referenced suppliers; the PO PDF prints the supplier address.

**Tech Stack:** Python 3.12, FastAPI, raw `sqlite3` (SQLite 3.45), Jinja2, fpdf2, pytest.

**Spec:** `docs/superpowers/specs/2026-10-16-single-supplier-store-design.md`

## Global Constraints

- Never edit a shipped migration; only add catalog v23 and purchase-orders v6. Feature order in `web/app.py` is unchanged.
- Migrations are pure functions of the connection they are handed; a failing migration must raise and leave the database unchanged (explicit `BEGIN`/`ROLLBACK` around every rebuild).
- `PRAGMA foreign_keys` must be OFF around a table rebuild (dropping `part_suppliers` with it on cascades into `part_supplier_tiers`) and back ON afterwards, with `PRAGMA foreign_key_check(<table>)` empty.
- `contacts.kind = 'supplier'` is the only definition of a supplier. All supplier dropdowns read `contacts`.
- Commit after every task (CLAUDE.md). UI follows `docs/design-rules.md`: errors render in `.err`.
- Tests construct databases with `Database(tmp_path / ...)` + `FeatureRegistry`; any test registry that includes `catalog_feature` and creates supplier lines must also register `contacts_feature` (after catalog).
- Run commands with `uv run`. Work in the worktree `/home/service1/partpilot-suppliers`.

## Review Focus

1. A part offer whose `supplier_id` is NULL (part with no supplier) must survive the rebuild as NULL and still render on the part page. Pinned in Task 2.
2. Two supplier contacts with the same name but different case must not both be picked as a mapping target; the mapping takes the lowest id and the migration still succeeds. Pinned in Task 2.
3. A customer contact that happens to share a supplier's name must never be chosen as the mapping target (kind filter). Pinned in Task 2.
4. Changing a referenced supplier's kind via a crafted POST must be refused server-side, not just hidden. Pinned in Task 4.
5. The PO PDF must not crash when the supplier contact has no address fields at all (every optional line blank). Pinned in Task 5.

---

## File structure

- `src/partpilot/web/core/registry.py` — `Migration.sql: str | Callable[[sqlite3.Connection], None]`.
- `src/partpilot/web/core/db.py` — callable dispatch, `backup_dir`, `_backup_before_migrations`, `rebuild_table`, `table_exists`.
- `src/partpilot/web/app.py`, `src/partpilot/cli.py` — pass `backup_dir`.
- `src/partpilot/web/features/catalog/supplier_merge.py` — **new**: `map_suppliers_to_contacts(conn)`, `assert_no_dangling(conn, table)`, `SUPPLIER_ID_EXPR`, `migrate_part_suppliers(conn)`.
- `src/partpilot/web/features/catalog/migrations.py` — v23 (callable).
- `src/partpilot/web/features/catalog/repo.py`, `importer.py` — contacts-based supplier helpers; `upsert_supplier` deleted.
- `src/partpilot/web/features/contacts/router.py`, `repo.py` — mirror deleted; kind guard.
- `src/partpilot/web/features/purchase_orders/supplier_merge.py` — **new**: `migrate_purchase_orders_and_grns(conn)`.
- `src/partpilot/web/features/purchase_orders/migrations.py` — v6 (callable).
- `src/partpilot/web/features/purchase_orders/repo.py`, `router.py`, `export.py`, `templates/purchase_order_detail.html`, `templates/purchase_order_form.html`.
- `src/partpilot/web/features/goods_receipts/repo.py`, `customer_orders/repo.py`, `setup/repo.py` — join `contacts`.
- Tests: `tests/test_db.py`, **new** `tests/test_supplier_merge.py`, edits to `tests/test_contacts.py`, `tests/test_web.py`, `tests/test_purchase_orders.py`, and the ten catalog-only fixtures.
- Docs: `README.md`, `CLAUDE.md`, `docs/design-rules.md`, `docs/user-manual.md`, memory `two-supplier-stores.md`.

---

### Task 1: Core — callable migrations, table rebuild helper, pre-migration backup

**Files:**
- Modify: `src/partpilot/web/core/registry.py:35-41`
- Modify: `src/partpilot/web/core/db.py`
- Modify: `src/partpilot/web/app.py:96`, `src/partpilot/cli.py:59`
- Test: `tests/test_db.py`

**Interfaces:**
- Produces: `Migration(version, name, sql: str | Callable[[sqlite3.Connection], None])`; `Database(db_path, backup_dir: Path | None = None)`; `Database.apply_migrations(registry) -> list[tuple[str,int]]` (backs up first when pending and not first run); `core.db.table_exists(conn, name) -> bool`; `core.db.rebuild_table(conn, table, *, sql_replacements: dict[str,str], column_exprs: dict[str,str]) -> None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_db.py`:

```python
import sqlite3
from pathlib import Path

from partpilot.web.core import Feature, FeatureRegistry, Migration
from partpilot.web.core.db import Database, rebuild_table, table_exists


def _feat(*migs):
    return Feature(name="t", migrations=list(migs))


def test_callable_migration_receives_connection(tmp_path):
    seen = {}

    def mig(conn):
        conn.execute("CREATE TABLE made (id INTEGER)")
        seen["ok"] = isinstance(conn, sqlite3.Connection)

    db = Database(tmp_path / "x.db")
    reg = FeatureRegistry(); reg.register(_feat(Migration(1, "callable", mig)))
    assert db.apply_migrations(reg) == [("t", 1)]
    assert seen["ok"]
    with db.connect() as conn:
        assert table_exists(conn, "made") and not table_exists(conn, "nope")
        assert conn.execute("SELECT 1 FROM schema_migrations WHERE feature='t' AND version=1").fetchone()


def test_backup_written_only_when_migrations_pending_and_not_first_run(tmp_path):
    bdir = tmp_path / "backups"
    db = Database(tmp_path / "x.db", backup_dir=bdir)
    reg = FeatureRegistry(); reg.register(_feat(Migration(1, "a", "CREATE TABLE a (id INTEGER);")))
    db.apply_migrations(reg)                     # first run: nothing to protect yet
    assert not bdir.exists() or not list(bdir.iterdir())
    db.apply_migrations(reg)                     # nothing pending
    assert not bdir.exists() or not list(bdir.iterdir())
    reg2 = FeatureRegistry()
    reg2.register(_feat(Migration(1, "a", "CREATE TABLE a (id INTEGER);"),
                        Migration(2, "b", "CREATE TABLE b (id INTEGER);")))
    db.apply_migrations(reg2)                    # pending on an existing DB -> backup
    files = list(bdir.glob("partpilot-*.db"))
    assert len(files) == 1
    with sqlite3.connect(files[0]) as c:          # the copy is a real database at the pre-state
        assert c.execute("SELECT name FROM sqlite_master WHERE name='a'").fetchone()
        assert not c.execute("SELECT name FROM sqlite_master WHERE name='b'").fetchone()


def test_backup_failure_aborts_before_migrating(tmp_path):
    blocker = tmp_path / "file.txt"; blocker.write_text("x")
    db = Database(tmp_path / "x.db", backup_dir=blocker / "backups")   # cannot mkdir under a file
    reg = FeatureRegistry(); reg.register(_feat(Migration(1, "a", "CREATE TABLE a (id INTEGER);")))
    db.apply_migrations(reg)                     # first run, no backup attempted
    reg2 = FeatureRegistry()
    reg2.register(_feat(Migration(1, "a", "CREATE TABLE a (id INTEGER);"),
                        Migration(2, "b", "CREATE TABLE b (id INTEGER);")))
    import pytest
    with pytest.raises(OSError):
        db.apply_migrations(reg2)
    with db.connect() as conn:
        assert not table_exists(conn, "b")        # migration 2 never ran


def test_rebuild_table_swaps_fk_and_keeps_rows_and_children(tmp_path):
    db = Database(tmp_path / "x.db")
    with db.connect() as conn:
        conn.executescript("""
            CREATE TABLE old_parent (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE new_parent (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES old_parent(id), note TEXT);
            CREATE INDEX ix_child_parent ON child(parent_id);
            CREATE TABLE grandchild (id INTEGER PRIMARY KEY, child_id INTEGER NOT NULL REFERENCES child(id) ON DELETE CASCADE);
            INSERT INTO old_parent VALUES (1,'a'),(2,'b');
            INSERT INTO new_parent VALUES (10,'a'),(20,'b');
            INSERT INTO child VALUES (1,1,'x'),(2,2,'y'),(3,NULL,'z');
            INSERT INTO grandchild VALUES (1,1),(2,2),(3,3);
        """)
        rebuild_table(conn, "child",
                      sql_replacements={"REFERENCES old_parent(id)": "REFERENCES new_parent(id)"},
                      column_exprs={"parent_id": "parent_id * 10"})
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='child'").fetchone()[0]
        assert "REFERENCES new_parent(id)" in sql and "old_parent" not in sql
        assert conn.execute("SELECT parent_id FROM child ORDER BY id").fetchall() == [(10,), (20,), (None,)]
        assert conn.execute("SELECT COUNT(*) FROM grandchild").fetchone()[0] == 3     # no cascade
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='ix_child_parent'").fetchone()
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert not conn.execute("PRAGMA foreign_key_check(child)").fetchall()


def test_rebuild_table_rolls_back_on_dangling_reference(tmp_path):
    db = Database(tmp_path / "x.db")
    with db.connect() as conn:
        conn.executescript("""
            CREATE TABLE new_parent (id INTEGER PRIMARY KEY);
            CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES new_parent(id));
            INSERT INTO new_parent VALUES (1);
            INSERT INTO child VALUES (1, 1), (2, 1);
        """)
        import pytest
        with pytest.raises(RuntimeError):
            rebuild_table(conn, "child", sql_replacements={}, column_exprs={"parent_id": "99"})
        assert conn.execute("SELECT parent_id FROM child ORDER BY id").fetchall() == [(1,), (1,)]
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name='child__new'").fetchone()
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_db.py -q`
Expected: FAIL / ERROR — `ImportError: cannot import name 'rebuild_table'`.

- [ ] **Step 3: Implement**

`registry.py`: change the dataclass field and import.

```python
import sqlite3
from typing import Callable
...
@dataclass(frozen=True)
class Migration:
    """One ordered schema change owned by a feature. ``sql`` is either a SQL script (several
    statements allowed) or a callable ``fn(conn)`` for migrations that must branch on the live
    schema (e.g. "does another feature's table exist yet?")."""

    version: int
    name: str
    sql: str | Callable[[sqlite3.Connection], None]
```

`db.py`: replace the class and add helpers.

```python
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from .registry import FeatureRegistry


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def rebuild_table(conn: sqlite3.Connection, table: str, *, sql_replacements: dict[str, str],
                  column_exprs: dict[str, str]) -> None:
    """Recreate ``table`` with its CREATE SQL textually edited (SQLite can't ALTER a foreign key),
    copying every row with selected columns rewritten by ``column_exprs`` (column -> SQL expr).

    Foreign-key enforcement is switched off for the duration: dropping the old table with it on
    would run ON DELETE actions on child tables. Runs in one explicit transaction and rolls back
    on any failure, including a non-empty ``foreign_key_check`` afterwards.
    """
    conn.commit()                                   # the pragma is a no-op inside a transaction
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN")
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if row is None:
            raise RuntimeError(f"rebuild_table: no table {table!r}")
        create_sql = row[0]
        for old, new in sql_replacements.items():
            create_sql = create_sql.replace(old, new)
        tmp = f"{table}__new"
        create_sql = re.sub(rf"CREATE TABLE\s+\"?{table}\"?", f"CREATE TABLE {tmp}", create_sql, count=1)
        indexes = [r[0] for r in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (table,))]
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        select = ", ".join(column_exprs.get(c, c) for c in cols)
        conn.execute(create_sql)
        conn.execute(f"INSERT INTO {tmp} ({', '.join(cols)}) SELECT {select} FROM {table}")
        n_old = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        n_new = conn.execute(f"SELECT COUNT(*) FROM {tmp}").fetchone()[0]
        if n_old != n_new:
            raise RuntimeError(f"rebuild_table: {table} copied {n_new} of {n_old} rows")
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {tmp} RENAME TO {table}")
        for ix in indexes:
            conn.execute(ix)
        bad = conn.execute(f"PRAGMA foreign_key_check({table})").fetchall()
        if bad:
            raise RuntimeError(f"rebuild_table: {len(bad)} dangling reference(s) in {table} after rebuild")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


class Database:
    def __init__(self, db_path: str | Path, backup_dir: str | Path | None = None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Where a copy of the database is written before migrations run. None disables (tests).
        self.backup_dir = Path(backup_dir) if backup_dir else None

    def connect(self) -> sqlite3.Connection:
        ...unchanged...

    def _backup(self, conn: sqlite3.Connection, pending: int) -> Path:
        """Online copy (WAL-safe) of the database into backup_dir; raises if it can't be written."""
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        dest = self.backup_dir / f"partpilot-{datetime.now():%Y%m%d-%H%M%S}.db"
        with sqlite3.connect(dest) as copy:
            conn.backup(copy)
        print(f"[partpilot] backed up database to {dest} before applying {pending} migration(s)")
        return dest

    def apply_migrations(self, registry: FeatureRegistry) -> list[tuple[str, int]]:
        """Apply every feature migration not yet recorded. Returns the ones applied.

        When migrations are pending on an existing database (one that already has a
        ``schema_migrations`` table) and ``backup_dir`` is set, the database is backed up first;
        a backup failure aborts before anything is applied.
        """
        applied: list[tuple[str, int]] = []
        with self.connect() as conn:
            first_run = not table_exists(conn, "schema_migrations")
            conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (...unchanged...)""")
            seen = {(r[0], r[1]) for r in conn.execute("SELECT feature, version FROM schema_migrations")}
            pending = [(f, m) for f, m in registry.all_migrations() if (f, m.version) not in seen]
            if pending and self.backup_dir and not first_run:
                conn.commit()
                self._backup(conn, len(pending))
            for feature_name, mig in pending:
                if callable(mig.sql):
                    mig.sql(conn)
                else:
                    conn.executescript(mig.sql)
                conn.execute("INSERT INTO schema_migrations (feature, version, name) VALUES (?, ?, ?)",
                             (feature_name, mig.version, mig.name))
                applied.append((feature_name, mig.version))
            conn.commit()
        return applied
```

`app.py:96`: `database = Database(resolved_db, backup_dir=resolved_data_dir / "backups")`.
`cli.py:59`: import `data_dir` too and use `Database(db_path(), backup_dir=data_dir() / "backups")`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_db.py -q`
Expected: all PASS (the two existing tests in the file too).

- [ ] **Step 5: Full suite, commit**

Run: `uv run pytest -q 2>&1 | tail -2` → Expected: all pass.

```bash
git add src/partpilot/web/core/registry.py src/partpilot/web/core/db.py src/partpilot/web/app.py src/partpilot/cli.py tests/test_db.py
git commit -m "Core: callable migrations, rebuild_table helper, backup before migrations"
```

---

### Task 2: Catalog v23 + every supplier read/write path moves to contacts

**Files:**
- Create: `src/partpilot/web/features/catalog/supplier_merge.py`
- Modify: `src/partpilot/web/features/catalog/migrations.py` (append v23)
- Modify: `src/partpilot/web/features/catalog/repo.py:56-66, 141-160, 228-230, 501-548, 600-611, 672-680`
- Modify: `src/partpilot/web/features/catalog/importer.py:43-56, 109-125`
- Modify: `src/partpilot/web/features/contacts/router.py` (delete `_mirror_supplier` + its two calls + the catalog import if unused — it is still used for shelves, keep it)
- Modify: `src/partpilot/web/features/purchase_orders/repo.py:31, 99-101, 119, 133`
- Modify: `src/partpilot/web/features/goods_receipts/repo.py:23, 36`
- Modify: `src/partpilot/web/features/customer_orders/repo.py:480`
- Modify: `src/partpilot/web/features/setup/repo.py:68`
- Modify: `src/partpilot/web/features/purchase_orders/templates/purchase_order_form.html:17-18`
- Test: `tests/test_supplier_merge.py` (new), `tests/test_contacts.py`, `tests/test_web.py`, `tests/test_purchase_orders.py`, `tests/test_catalog.py`, and fixtures in `tests/test_article_register.py`, `test_assemblies.py`, `test_devmgmt_model.py`, `test_documents.py`, `test_import_parts_engine.py`, `test_shelves_core.py`, `test_shelves_review_fixes.py`, `test_versions.py`, `test_woo_sync.py`

**Interfaces:**
- Consumes: `core.db.rebuild_table`, `core.db.table_exists`, `Migration` callable (Task 1).
- Produces: `catalog.supplier_merge.map_suppliers_to_contacts(conn) -> None` (adds/fills `suppliers.contact_id`, creates missing supplier contacts); `catalog.supplier_merge.assert_no_dangling(conn, table) -> None`; `catalog.supplier_merge.SUPPLIER_ID_EXPR = "(SELECT s.contact_id FROM suppliers s WHERE s.id = supplier_id)"`; `catalog.supplier_merge.FK_SWAP = {"REFERENCES suppliers(id)": "REFERENCES contacts(id)"}`; `catalog.repo.suppliers(db) -> list[{id, name}]` from contacts; `catalog.repo._get_or_create_supplier(conn, name) -> int` (contact id).

- [ ] **Step 1: Write the failing migration tests**

Create `tests/test_supplier_merge.py`:

```python
"""Dropping the catalog `suppliers` table: contacts (kind=supplier) become the only supplier record."""

import dataclasses

import pytest

from partpilot.web.core import FeatureRegistry
from partpilot.web.core.db import Database, table_exists
from partpilot.web.features.catalog import feature as catalog_feature
from partpilot.web.features.contacts import feature as contacts_feature
from partpilot.web.features.customer_orders import feature as co_feature
from partpilot.web.features.purchase_orders import feature as po_feature
from partpilot.web.features.work_orders import feature as wo_feature
from partpilot.web.features.assemblies import feature as asm_feature

OLD = [dataclasses.replace(catalog_feature, migrations=catalog_feature.migrations[:22]),
       asm_feature, contacts_feature, co_feature, wo_feature,
       dataclasses.replace(po_feature, migrations=po_feature.migrations[:5])]
NEW = [catalog_feature, asm_feature, contacts_feature, co_feature, wo_feature, po_feature]


def _old_db(tmp_path):
    db = Database(tmp_path / "live.db")
    reg = FeatureRegistry(); reg.register(*OLD)
    db.apply_migrations(reg)
    return db


def _seed(conn):
    """Live-shaped data: 2 pairs linked by minimrp_id, 1 by name (case differs), 1 orphan supplier,
    a customer sharing an orphan's name, two same-named supplier contacts, an offer with no supplier."""
    conn.executescript("""
        INSERT INTO contacts (id, kind, name, minimrp_id, source, currency) VALUES
          (101,'supplier','Farnell',   7, 'sup','GBP'),
          (102,'supplier','RS',        8, 'sup','SEK'),
          (103,'supplier','Acme Parts', NULL,'ui', NULL),
          (104,'customer','Digi-Key',  NULL,'ui', NULL),
          (105,'supplier','Dupe Co',   NULL,'ui', NULL),
          (106,'supplier','DUPE CO',   NULL,'ui', NULL);
        INSERT INTO suppliers (id, name, short_name, url, currency, minimrp_id) VALUES
          (1,'Farnell (old name)', NULL, NULL, 'GBP', 7),
          (2,'RS',                 NULL, NULL, 'SEK', 8),
          (3,'ACME PARTS ',        NULL, NULL, NULL, NULL),
          (4,'Digi-Key',           'DK', 'https://digikey.se', 'SEK', NULL),
          (5,'dupe co',            NULL, NULL, NULL, NULL);
        INSERT INTO parts (id, part_no) VALUES (1,'P1'),(2,'P2'),(3,'P3');
        INSERT INTO part_suppliers (id, part_id, supplier_id, supplier_pno, qty_per_uom, is_default) VALUES
          (1,1,1,'F-1',1,1),(2,1,4,'DK-1',1,0),(3,2,3,'A-1',1,1),(4,3,NULL,NULL,1,1),(5,2,5,'D-1',1,0);
        INSERT INTO part_supplier_tiers (part_supplier_id, kind, break_qty, unit_cost) VALUES
          (1,'cut_tape',1,0.5),(1,'cut_tape',100,0.4),(2,'cut_tape',1,0.9);
        INSERT INTO purchase_orders (id, po_no, supplier_id, status) VALUES (1,'PO-1',4,'ordered'),(2,'PO-2',2,'draft');
        INSERT INTO goods_receipts (id, grn_no, po_id, supplier_id) VALUES (1,'GRN-1',1,4);
    """)
    conn.commit()


def test_migration_maps_every_supplier_to_a_contact_and_drops_the_table(tmp_path):
    db = _old_db(tmp_path)
    with db.connect() as conn:
        _seed(conn)
    reg = FeatureRegistry(); reg.register(*NEW)
    db.apply_migrations(reg)
    with db.connect() as conn:
        assert not table_exists(conn, "suppliers")
        ps = dict(conn.execute("SELECT id, supplier_id FROM part_suppliers").fetchall())
        dk = conn.execute("SELECT id FROM contacts WHERE kind='supplier' AND name='Digi-Key'").fetchone()[0]
        assert dk not in (101, 102, 103, 104, 105, 106)          # a NEW supplier contact, not the customer
        assert ps == {1: 101, 2: dk, 3: 103, 4: None, 5: 105}      # minimrp id / name (trimmed, case) / orphan / NULL / lowest dupe
        row = conn.execute("SELECT short_name, website, currency, source FROM contacts WHERE id=?", (dk,)).fetchone()
        assert tuple(row) == ("DK", "https://digikey.se", "SEK", "sup-merge")
        assert conn.execute("SELECT COUNT(*) FROM part_supplier_tiers").fetchone()[0] == 3
        assert dict(conn.execute("SELECT id, supplier_id FROM purchase_orders").fetchall()) == {1: dk, 2: 102}
        assert conn.execute("SELECT supplier_id FROM goods_receipts").fetchone()[0] == dk
        for t in ("part_suppliers", "purchase_orders", "goods_receipts"):
            sql = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (t,)).fetchone()[0]
            assert "REFERENCES contacts(id)" in sql and "suppliers(id)" not in sql
            assert not conn.execute(f"PRAGMA foreign_key_check({t})").fetchall()
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert {"ix_partsup_part", "ix_po_supplier", "ix_po_status", "ix_grn_po"} <= names
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_fresh_database_ends_with_contacts_fks_and_no_suppliers_table(tmp_path):
    db = Database(tmp_path / "fresh.db")
    reg = FeatureRegistry(); reg.register(*NEW)
    db.apply_migrations(reg)
    with db.connect() as conn:
        assert not table_exists(conn, "suppliers")
        for t in ("part_suppliers", "purchase_orders", "goods_receipts"):
            assert "REFERENCES contacts(id)" in conn.execute(
                "SELECT sql FROM sqlite_master WHERE name=?", (t,)).fetchone()[0]


def test_catalog_only_registry_still_migrates(tmp_path):
    """Test registries that register catalog without contacts must keep working (no rows to map)."""
    db = Database(tmp_path / "c.db")
    reg = FeatureRegistry(); reg.register(catalog_feature)
    db.apply_migrations(reg)
    with db.connect() as conn:
        assert "REFERENCES contacts(id)" in conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='part_suppliers'").fetchone()[0]


def test_dangling_supplier_reference_fails_the_migration_and_changes_nothing(tmp_path):
    db = _old_db(tmp_path)
    with db.connect() as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.executescript("""
            INSERT INTO parts (id, part_no) VALUES (1,'P1');
            INSERT INTO part_suppliers (id, part_id, supplier_id, qty_per_uom) VALUES (1,1,77,1);
        """)
        conn.commit()
    reg = FeatureRegistry(); reg.register(*NEW)
    with pytest.raises(RuntimeError, match="dangling"):
        db.apply_migrations(reg)
    with db.connect() as conn:
        assert table_exists(conn, "suppliers")
        assert "REFERENCES suppliers(id)" in conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='part_suppliers'").fetchone()[0]
        assert not conn.execute("SELECT 1 FROM schema_migrations WHERE feature='catalog' AND version=23").fetchone()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_supplier_merge.py -q`
Expected: 4 FAIL (table `suppliers` still exists; `REFERENCES contacts(id)` absent).

Note: the first and fourth tests also need Task 3's purchase-orders v6 to go fully green; after this task expect `test_migration_maps_every_supplier…` to fail only on `not table_exists(conn, "suppliers")` / the PO assertions, and the fresh/catalog-only tests to pass. Record that in the ledger; Task 3 turns them green.

- [ ] **Step 3: Write `catalog/supplier_merge.py`**

```python
"""Merge the legacy catalog ``suppliers`` table into Contacts (kind='supplier').

History: miniMRP had one supplier table; PartPilot loaded it twice on 2026-06-11 (thin
``suppliers`` for procurement FKs, full ``contacts`` for the address book). Catalog migration 23
and purchase-orders migration 6 use these helpers to re-point every FK at ``contacts(id)``.
"""

from __future__ import annotations

import sqlite3

from ...core.db import rebuild_table, table_exists

SUPPLIER_ID_EXPR = "(SELECT s.contact_id FROM suppliers s WHERE s.id = supplier_id)"
FK_SWAP = {"REFERENCES suppliers(id)": "REFERENCES contacts(id)"}


def _has_column(conn, table, col) -> bool:
    return any(r[1] == col for r in conn.execute(f"PRAGMA table_info({table})"))


def assert_no_dangling(conn: sqlite3.Connection, table: str) -> None:
    """Every non-NULL supplier_id in ``table`` must name an existing suppliers row."""
    n = conn.execute(
        f"SELECT COUNT(*) FROM {table} t WHERE t.supplier_id IS NOT NULL "
        f"AND NOT EXISTS (SELECT 1 FROM suppliers s WHERE s.id = t.supplier_id)").fetchone()[0]
    if n:
        raise RuntimeError(f"{n} row(s) in {table} have a dangling supplier_id; fix them before migrating")


def map_suppliers_to_contacts(conn: sqlite3.Connection) -> None:
    """Give every ``suppliers`` row a ``contact_id``: same miniMRP id, else same trimmed
    case-insensitive name among supplier contacts (lowest id wins), else a new supplier contact."""
    if not _has_column(conn, "suppliers", "contact_id"):
        conn.execute("ALTER TABLE suppliers ADD COLUMN contact_id INTEGER")
    conn.execute("""
        UPDATE suppliers SET contact_id = (
            SELECT c.id FROM contacts c WHERE c.kind = 'supplier'
              AND c.minimrp_id IS NOT NULL AND c.minimrp_id = suppliers.minimrp_id
            ORDER BY c.id LIMIT 1)
        WHERE contact_id IS NULL""")
    conn.execute("""
        UPDATE suppliers SET contact_id = (
            SELECT c.id FROM contacts c WHERE c.kind = 'supplier'
              AND lower(trim(c.name)) = lower(trim(suppliers.name))
            ORDER BY c.id LIMIT 1)
        WHERE contact_id IS NULL""")
    for row in conn.execute(
            "SELECT id, name, short_name, url, currency, minimrp_id FROM suppliers WHERE contact_id IS NULL").fetchall():
        cid = conn.execute(
            """INSERT INTO contacts (kind, name, short_name, website, currency, minimrp_id, source)
               VALUES ('supplier', ?, ?, ?, ?, ?, 'sup-merge')""",
            (row["name"].strip() or "?", row["short_name"], row["url"], row["currency"], row["minimrp_id"]),
        ).lastrowid
        conn.execute("UPDATE suppliers SET contact_id = ? WHERE id = ?", (cid, row["id"]))
    conn.commit()


def migrate_part_suppliers(conn: sqlite3.Connection) -> None:
    """Catalog migration 23. On a fresh database (no contacts table yet) only the FK is swapped."""
    if not table_exists(conn, "contacts"):
        rebuild_table(conn, "part_suppliers", sql_replacements=FK_SWAP, column_exprs={})
        return
    assert_no_dangling(conn, "part_suppliers")
    map_suppliers_to_contacts(conn)
    rebuild_table(conn, "part_suppliers", sql_replacements=FK_SWAP,
                  column_exprs={"supplier_id": SUPPLIER_ID_EXPR})
```

Note: `conn.row_factory` is `sqlite3.Row` from `Database.connect()`, so `row["name"]` works. `minimrp_id` on contacts has a unique index on `(source, minimrp_id)`; `'sup-merge'` is a distinct source so no collision.

Append to `catalog/migrations.py`:

```python
    Migration(
        version=23,
        name="suppliers merge: part_suppliers.supplier_id -> contacts(id)",
        sql=_migrate_part_suppliers,
    ),
```

with, near the top of the file, a thin indirection so the migration module doesn't import repo code at import time:

```python
def _migrate_part_suppliers(conn):
    from .supplier_merge import migrate_part_suppliers
    migrate_part_suppliers(conn)
```

- [ ] **Step 4: Switch the catalog repo and importer to contacts**

`catalog/repo.py`:
- line 64: `LEFT JOIN contacts s ON s.id = ps.supplier_id`
- line 151: `JOIN contacts s ON s.id = ps.supplier_id`
- `suppliers()`:

```python
def suppliers(db: Database) -> list[dict]:
    """Supplier dropdown: every supplier-kind contact. Contacts is the only supplier record."""
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, name FROM contacts WHERE kind = 'supplier' ORDER BY name")]
```

- `_get_or_create_supplier`:

```python
def _get_or_create_supplier(conn, name: str) -> int:
    """Id of the supplier contact called ``name`` (trimmed, case-insensitive), creating a bare one
    ("+ New supplier…" on the part form, or the resolver meeting Digi-Key for the first time)."""
    name = (name or "").strip()
    row = conn.execute(
        "SELECT id FROM contacts WHERE kind = 'supplier' AND lower(trim(name)) = lower(?) ORDER BY id LIMIT 1",
        (name,)).fetchone()
    if row:
        return row["id"]
    return conn.execute("INSERT INTO contacts (kind, name, source) VALUES ('supplier', ?, 'ui')", (name,)).lastrowid
```

- Delete `upsert_supplier` entirely (lines 508-548).

`catalog/importer.py` suppliers block (lines 43-56) becomes:

```python
        # --- suppliers (legacy tblsupaddresses rows) → supplier contacts, keyed like contacts.importer ---
        for r in suppliers:
            conn.execute(
                """INSERT INTO contacts (kind, name, short_name, website, currency, minimrp_id, source)
                   VALUES ('supplier', ?, ?, ?, ?, ?, 'sup')
                   ON CONFLICT(source, minimrp_id) DO UPDATE SET
                     name=excluded.name, short_name=excluded.short_name,
                     website=excluded.website, currency=excluded.currency""",
                (_s(r.get("CoName")) or "?", _s(r.get("ShortNm")), _s(r.get("URL")),
                 _s(r.get("defCurrency")), _i(r.get("AddID"))),
            )
        sup_map = {row["minimrp_id"]: row["id"] for row in conn.execute(
            "SELECT id, minimrp_id FROM contacts WHERE source = 'sup' AND minimrp_id IS NOT NULL")}
```

Update the module docstring line 3 to say "supplier contacts".

`contacts/router.py`: delete `_mirror_supplier` (lines 18-27) and its two call sites (`create`, `update`). Keep the `catalog_repo` import (used for shelves).

Other joins, one-word edits `suppliers` → `contacts`: `purchase_orders/repo.py:31,119,133`; `purchase_orders/repo.py:99-101` becomes the same body as `catalog.repo.suppliers`; `goods_receipts/repo.py:23,36`; `customer_orders/repo.py:480`; `setup/repo.py:68`.

`purchase_order_form.html:17-18`: text → `No suppliers yet — add a supplier in Contacts (kind "supplier") or on a part.`

- [ ] **Step 5: Fix the test fixtures and rewrite obsolete tests**

Add `contacts_feature` (imported from `partpilot.web.features.contacts import feature as contacts_feature`) right after `catalog_feature` in the registries of: `tests/test_article_register.py:17`, `test_assemblies.py:14`, `test_catalog.py:13` and `:444`, `test_devmgmt_model.py:15`, `test_documents.py:18`, `test_import_parts_engine.py:15`, `test_shelves_core.py:14`, `test_shelves_review_fixes.py` (the `older`/`full` registries in `test_migration_22_…` only if they create supplier lines — they don't; leave), `test_versions.py:22`, `test_woo_sync.py:15`.

`tests/test_contacts.py`: delete `test_upsert_supplier_matches_and_never_wipes`, `test_upsert_supplier_matches_by_minimrp_id_across_rename`, `test_backfill_migration_seeds_suppliers_from_supplier_contacts`; fix the fixture comment (lines 14-15) to "catalog first (as in production); part_suppliers references contacts".

`tests/test_web.py:272-301`: replace the mirror test with:

```python
def test_supplier_contact_is_the_supplier_dropdown(app):
    """Contacts is the only supplier record: a supplier-kind contact is immediately selectable on the
    part form and PO form; a customer is not."""
    from partpilot.web.features.catalog import repo as catrepo

    client = TestClient(app)
    _login(client, "buyer1", "pw")
    db = app.state.database
    client.post("/contacts/new", data={"kind": "supplier", "name": "Widget Supply AB", "currency": "SEK"},
                follow_redirects=False)
    client.post("/contacts/new", data={"kind": "customer", "name": "Buyer Co"}, follow_redirects=False)
    names = [s["name"] for s in catrepo.suppliers(db)]
    assert "Widget Supply AB" in names and "Buyer Co" not in names
    assert "Widget Supply AB" in client.get("/catalog/new").text
    assert "Widget Supply AB" in client.get("/purchase-orders/new").text
```

`tests/test_web.py:1330`: `sid = c.execute("INSERT INTO contacts (kind, name) VALUES ('supplier', ?)", (supplier,)).lastrowid`.

`tests/test_purchase_orders.py:312`: replace `catrepo.upsert_supplier(db, name="Beta Ltd")` with `conrepo.create_contact(db, {"kind": "supplier", "name": "Beta Ltd"})` (add `from partpilot.web.features.contacts import repo as conrepo`).

- [ ] **Step 6: Run the suite**

Run: `uv run pytest -q 2>&1 | tail -3`
Expected: everything passes except, in `tests/test_supplier_merge.py`, `test_migration_maps_every_supplier…` (suppliers table still present; PO ids unmapped) and `test_fresh_database_ends_with…` (PO tables still reference suppliers). Those two are Task 3's red bar. No other failures.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "Suppliers merge (1/2): catalog v23 re-points part_suppliers at contacts; all supplier reads/writes use contacts; mirror-on-save removed"
```

---

### Task 3: Purchase-orders v6 — rebuild POs and GRNs, drop `suppliers`

**Files:**
- Create: `src/partpilot/web/features/purchase_orders/supplier_merge.py`
- Modify: `src/partpilot/web/features/purchase_orders/migrations.py` (append v6)
- Test: `tests/test_supplier_merge.py` (already written in Task 2)

**Interfaces:**
- Consumes: `catalog.supplier_merge.{assert_no_dangling, map_suppliers_to_contacts, SUPPLIER_ID_EXPR, FK_SWAP}`, `core.db.{rebuild_table, table_exists}`.
- Produces: `purchase_orders.supplier_merge.migrate_purchase_orders_and_grns(conn)`.

- [ ] **Step 1: Confirm the red bar**

Run: `uv run pytest tests/test_supplier_merge.py -q`
Expected: 2 FAIL (`test_migration_maps_every_supplier…`, `test_fresh_database_ends_with…`), 2 PASS.

- [ ] **Step 2: Implement**

`purchase_orders/supplier_merge.py`:

```python
"""Purchase-orders migration 6: purchase_orders/goods_receipts.supplier_id -> contacts(id), then
drop the legacy ``suppliers`` table. Runs after catalog 23 and the contacts migrations."""

from __future__ import annotations

import sqlite3

from ...core.db import rebuild_table, table_exists
from ..catalog.supplier_merge import FK_SWAP, SUPPLIER_ID_EXPR, assert_no_dangling, map_suppliers_to_contacts

_TABLES = ("purchase_orders", "goods_receipts")


def migrate_purchase_orders_and_grns(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "suppliers"):
        return                                           # already merged
    has_rows = conn.execute("SELECT 1 FROM suppliers LIMIT 1").fetchone() is not None
    if has_rows and table_exists(conn, "contacts"):
        for t in _TABLES:
            assert_no_dangling(conn, t)
        map_suppliers_to_contacts(conn)                  # idempotent; catalog 23 normally did this
        exprs = {"supplier_id": SUPPLIER_ID_EXPR}
    else:
        exprs = {}                                       # fresh database: nothing to map
    for t in _TABLES:
        rebuild_table(conn, t, sql_replacements=FK_SWAP, column_exprs=exprs)
    unmapped = conn.execute(
        "SELECT COUNT(*) FROM suppliers WHERE contact_id IS NULL").fetchone()[0] if has_rows else 0
    if unmapped:
        raise RuntimeError(f"{unmapped} supplier row(s) could not be mapped to a contact")
    conn.execute("DROP TABLE suppliers")
    conn.commit()
```

Append to `purchase_orders/migrations.py`:

```python
def _migrate_suppliers(conn):
    from .supplier_merge import migrate_purchase_orders_and_grns
    migrate_purchase_orders_and_grns(conn)
...
    Migration(
        version=6,
        name="suppliers merge: purchase_orders/goods_receipts.supplier_id -> contacts(id); drop suppliers",
        sql=_migrate_suppliers,
    ),
```

- [ ] **Step 3: Run the merge tests, then the suite**

Run: `uv run pytest tests/test_supplier_merge.py -q` → Expected: 4 PASS.
Run: `uv run pytest -q 2>&1 | tail -2` → Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add src/partpilot/web/features/purchase_orders/supplier_merge.py src/partpilot/web/features/purchase_orders/migrations.py
git commit -m "Suppliers merge (2/2): PO v6 re-points purchase_orders/goods_receipts at contacts and drops the suppliers table"
```

---

### Task 4: Refuse changing a referenced supplier's kind

**Files:**
- Modify: `src/partpilot/web/features/contacts/repo.py:58-66`
- Modify: `src/partpilot/web/features/contacts/router.py` (`update`)
- Test: `tests/test_contacts.py`, `tests/test_web.py`

**Interfaces:**
- Produces: `contacts.repo.supplier_references(db, contact_id) -> dict[str,int]` (`{"parts": n, "purchase_orders": m, "goods_receipts": k}`, missing tables count 0); `contacts.repo.update_contact` raises `ValueError(message)` when `kind` leaves `supplier` while referenced.

- [ ] **Step 1: Failing tests**

Append to `tests/test_contacts.py`:

```python
def test_kind_change_refused_while_used_as_supplier(db):
    from partpilot.web.features.catalog import repo as catrepo
    sid = repo.create_contact(db, {"kind": "supplier", "name": "Acme Parts"})
    catrepo.create_part(db, part={"part_no": "R-1"},
                        supplier_lines=[{"supplier_name": "Acme Parts", "supplier_pno": "A", "unit_price": 0.1,
                                         "reel_qty": 1, "is_default": True}], opening=None)
    assert repo.supplier_references(db, sid) == {"parts": 1, "purchase_orders": 0, "goods_receipts": 0}
    with pytest.raises(ValueError, match="1 part"):
        repo.update_contact(db, sid, {"kind": "customer", "name": "Acme Parts"})
    assert repo.get_contact(db, sid)["kind"] == "supplier"
    repo.update_contact(db, sid, {"kind": "supplier", "name": "Acme Parts Ltd"})   # same kind: fine
    unused = repo.create_contact(db, {"kind": "supplier", "name": "Nobody"})
    repo.update_contact(db, unused, {"kind": "other", "name": "Nobody"})           # unreferenced: fine
```

(add `import pytest` at the top of the file if absent). Append to `tests/test_web.py` after the dropdown test:

```python
def test_kind_change_of_referenced_supplier_shows_error(app):
    from partpilot.web.features.catalog import repo as catrepo
    from partpilot.web.features.contacts import repo as conrepo
    client = TestClient(app); _login(client, "buyer1", "pw")
    db = app.state.database
    sid = conrepo.create_contact(db, {"kind": "supplier", "name": "Digi-Key"})
    catrepo.create_part(db, part={"part_no": "C-1"},
                        supplier_lines=[{"supplier_name": "Digi-Key", "supplier_pno": "D", "unit_price": 0.1,
                                         "reel_qty": 1, "is_default": True}], opening=None)
    r = client.post(f"/contacts/{sid}/edit", data={"kind": "customer", "name": "Digi-Key"})
    assert r.status_code == 400 and "must stay a supplier" in r.text
    assert conrepo.get_contact(db, sid)["kind"] == "supplier"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_contacts.py::test_kind_change_refused_while_used_as_supplier tests/test_web.py::test_kind_change_of_referenced_supplier_shows_error -q`
Expected: FAIL — `AttributeError: module ... has no attribute 'supplier_references'`.

- [ ] **Step 3: Implement**

`contacts/repo.py`:

```python
from ...core.db import Database, table_exists

_REFERENCING = (("parts", "part_suppliers"), ("purchase_orders", "purchase_orders"),
                ("goods_receipts", "goods_receipts"))


def supplier_references(db: Database, contact_id: int) -> dict[str, int]:
    """How many part offers / POs / GRNs point at this contact as their supplier."""
    out = {}
    with db.connect() as conn:
        for label, table in _REFERENCING:
            out[label] = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE supplier_id = ?", (contact_id,)).fetchone()[0] \
                if table_exists(conn, table) else 0
    return out


def update_contact(db: Database, contact_id: int, data: dict) -> None:
    current = get_contact(db, contact_id)
    if current and current["kind"] == "supplier" and data.get("kind") != "supplier":
        refs = supplier_references(db, contact_id)
        if any(refs.values()):
            raise ValueError(
                f"{current['name']} is used as a supplier on {refs['parts']} part(s), "
                f"{refs['purchase_orders']} purchase order(s) and {refs['goods_receipts']} goods "
                f"receipt(s) — it must stay a supplier.")
    assignments = ", ".join(f"{f} = ?" for f in _FIELDS)
    ...unchanged body...
```

`contacts/router.py` `update`: wrap the repo call:

```python
    try:
        repo.update_contact(db, contact_id, data)
    except ValueError as e:
        return _render_form(
            request, action=f"/contacts/{contact_id}/edit", heading="Edit contact",
            submit_label="Save changes", back_url=f"/contacts/{contact_id}", values=data,
            error=str(e), status=400,
        )
    return RedirectResponse(f"/contacts/{contact_id}", status_code=303)
```

- [ ] **Step 4: Run tests, suite, commit**

Run: `uv run pytest tests/test_contacts.py tests/test_web.py -q` → PASS. `uv run pytest -q | tail -2` → all pass.

```bash
git add src/partpilot/web/features/contacts tests/test_contacts.py tests/test_web.py
git commit -m "Contacts: refuse changing a referenced supplier's kind"
```

---

### Task 5: PO PDF supplier address, detail link, currency default

**Files:**
- Modify: `src/partpilot/web/features/purchase_orders/repo.py` (`get_po`, `create_po`)
- Modify: `src/partpilot/web/features/purchase_orders/export.py:102-108`
- Modify: `src/partpilot/web/features/purchase_orders/templates/purchase_order_detail.html:28`
- Test: `tests/test_purchase_orders.py`

**Interfaces:**
- Consumes: `contacts` columns `address, postcode, country, org_no, email, phone, currency`.
- Produces: `get_po()` head gains `supplier_address, supplier_postcode, supplier_country, supplier_org_no, supplier_email, supplier_phone`; `create_po()` fills `currency` from the supplier contact when blank.

- [ ] **Step 1: Failing tests**

Append to `tests/test_purchase_orders.py`:

```python
def test_po_pdf_prints_supplier_address_and_survives_blank_contact(db):
    from partpilot.web.features.contacts import repo as conrepo
    from partpilot.web.features.purchase_orders import export
    full = conrepo.create_contact(db, {"kind": "supplier", "name": "Full Supplier", "address": "Storgatan 1\nBox 9",
                                       "postcode": "123 45 Lund", "country": "Sweden", "org_no": "556677-8899",
                                       "email": "sales@full.example", "phone": "+46 46 123", "currency": "EUR"})
    bare = conrepo.create_contact(db, {"kind": "supplier", "name": "Bare Supplier"})
    po_full = repo.create_po(db, {"supplier_id": full})
    po_bare = repo.create_po(db, {"supplier_id": bare, "currency": "SEK"})
    head = repo.get_po(db, po_full)
    assert head["supplier_address"] == "Storgatan 1\nBox 9" and head["supplier_org_no"] == "556677-8899"
    assert head["currency"] == "EUR"                       # defaulted from the contact
    assert repo.get_po(db, po_bare)["currency"] == "SEK"    # explicit value wins
    pdf = export.po_pdf(head, {})
    assert pdf[:5] == b"%PDF-"
    text = _pdf_text(pdf)
    for needle in ("Storgatan 1", "Box 9", "123 45 Lund", "Sweden", "Org no: 556677-8899", "sales@full.example", "+46 46 123"):
        assert needle in text
    assert export.po_pdf(repo.get_po(db, po_bare), {})[:5] == b"%PDF-"   # every optional line blank: no crash


def _pdf_text(pdf: bytes) -> str:
    """Decode fpdf's uncompressed-or-flate page streams enough to grep for literal strings."""
    import re, zlib
    out = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", pdf, re.S):
        data = m.group(1)
        try:
            data = zlib.decompress(data)
        except zlib.error:
            pass
        out.append(data.decode("latin-1", "replace"))
    return "\n".join(out)
```

Check how `export.po_pdf` is invoked in existing tests (`grep -n "po_pdf(" tests/test_purchase_orders.py`) and match its signature; if a text-extraction helper already exists in the test file, reuse it instead of `_pdf_text`.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_purchase_orders.py::test_po_pdf_prints_supplier_address_and_survives_blank_contact -q`
Expected: FAIL — `KeyError: 'supplier_address'`.

- [ ] **Step 3: Implement**

`repo.get_po` head query:

```sql
SELECT p.*, s.name AS supplier_name, s.address AS supplier_address, s.postcode AS supplier_postcode,
       s.country AS supplier_country, s.org_no AS supplier_org_no, s.email AS supplier_email,
       s.phone AS supplier_phone, loc.name AS location_name, loc.kind AS location_kind
FROM purchase_orders p
LEFT JOIN contacts s ON s.id = p.supplier_id
LEFT JOIN stock_locations loc ON loc.id = p.location_id WHERE p.id = ?
```

`repo.create_po`: before the INSERT,

```python
        currency = data.get("currency")
        if not currency and data.get("supplier_id"):
            row = conn.execute("SELECT currency FROM contacts WHERE id = ?", (data["supplier_id"],)).fetchone()
            currency = row["currency"] if row else None
```

and use `currency` in the INSERT parameters.

`export.py` supplier block (replace lines 105-107):

```python
    pdf.cell(0, 6, "Supplier", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 5, _s(po.get("supplier_name") or "-"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 9)
    for block in [po.get("supplier_address"), po.get("supplier_postcode"), po.get("supplier_country"),
                  _kv("Org no", po.get("supplier_org_no")),
                  _join_sep(po.get("supplier_email"), po.get("supplier_phone"))]:
        for line in (block or "").splitlines():
            if line.strip():
                pdf.cell(0, 5, _s(line.strip()), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
```

(`_kv` and `_join_sep` already exist in the file for the issuer block; confirm with `grep -n "def _kv\|def _join_sep" export.py`.)

`purchase_order_detail.html:28`:

```html
  <p class="note">{% if po.supplier_id %}<a href="/contacts/{{ po.supplier_id }}">{{ po.supplier_name }}</a>{% else %}No supplier{% endif %}
```

- [ ] **Step 4: Run tests, suite, commit**

Run: `uv run pytest tests/test_purchase_orders.py -q` → PASS; `uv run pytest -q | tail -2` → all pass.

```bash
git add src/partpilot/web/features/purchase_orders tests/test_purchase_orders.py
git commit -m "Purchase orders: supplier address on the PDF, contact link on the PO page, currency from the supplier contact"
```

---

### Task 6: Documentation and memory

**Files:**
- Modify: `README.md` (Contacts paragraph ~142-143; Parts paragraph; Architecture "migrations" sentence), `CLAUDE.md` (Architecture → Migrations: callable + backup; remove/adjust any "two supplier stores" wording), `docs/design-rules.md` (change log), `docs/user-manual.md` (Contacts → Suppliers; Buying → Placing the order; FAQ), memory file `/home/service1/.claude/projects/-home-service1-partpilot/memory/two-supplier-stores.md` + `MEMORY.md` line.

- [ ] **Step 1: README**

Replace the Contacts sentence "Saving a supplier contact mirrors it into the catalog's `suppliers` table, which the part-edit and PO forms read." with: "Contacts is the only supplier record: the part-edit and PO supplier dropdowns list supplier-kind contacts, and the PO PDF prints the supplier's address from here. (The legacy catalog `suppliers` table was merged away on 2026-10-16.)" In the Architecture section add: "Migrations are SQL scripts or Python callables; the runner backs the database up to `data/backups/` before applying any pending migration on an existing database."

- [ ] **Step 2: CLAUDE.md**

In "Architecture → The web app → Migrations" bullet add: "A `Migration.sql` may be a callable `fn(conn)` when the step must inspect the live schema (see `catalog/supplier_merge.py`). Before applying pending migrations on an existing database the runner writes `data/backups/partpilot-<ts>.db`; a failed backup aborts startup." Add a line to the Deployment section: "Every restart that applies migrations leaves a backup in `data/backups/` — prune it by hand."

- [ ] **Step 3: design-rules change log**

Append: "**2026-10-16** — **Suppliers live only in Contacts.** The catalog `suppliers` table is gone; supplier `<select>`s list `contacts WHERE kind='supplier'` via `catalog.repo.suppliers()` / `purchase_orders.repo.suppliers()`. The PO page links the supplier to its contact; the PO PDF prints the contact's address block under 'Supplier'. Refusing a kind change on a referenced supplier renders in `.err` on the contact form."

- [ ] **Step 4: User manual**

In *Contacts → Suppliers*: replace with "A supplier is a contact of kind supplier. It is the only place supplier details live: the supplier dropdowns on parts and purchase orders list these contacts, and the purchase order PDF prints the address, organisation number, email and phone from here. Fill in **Currency** so new purchase orders default to it. A contact that is used as a supplier on any part, purchase order or goods receipt cannot be changed to another kind." In *Buying → Placing the order*, after "The PDF carries our company details from Setup", add "and the supplier's address from Contacts". In *Parts → Adding or editing a part*, keep "Suppliers added here also appear in Contacts as supplier contacts" and add "with only a name; complete the address in Contacts."

- [ ] **Step 5: Memory**

Rewrite `two-supplier-stores.md` body: "Resolved 2026-10-16: the catalog `suppliers` table was dropped (catalog v23 + purchase-orders v6); `part_suppliers`, `purchase_orders`, `goods_receipts` reference `contacts(id)`; Contacts kind='supplier' is the single supplier record. Backstory: miniMRP's one `tblsupaddresses` was imported twice on 2026-06-11. **How to apply:** supplier dropdowns = `catalog.repo.suppliers()`; new suppliers via `_get_or_create_supplier` create a contact; never reintroduce a parallel supplier table." Update the `MEMORY.md` hook text to "merged 2026-10-16; contacts is the only supplier record".

- [ ] **Step 6: Commit**

```bash
git add README.md CLAUDE.md docs/design-rules.md docs/user-manual.md
git commit -m "Docs: suppliers live only in Contacts; callable migrations and pre-migration backups"
```

---

## Self-review

- Spec §3.1 → Task 1; §3.2 → Task 2; §3.3–3.4 → Tasks 2–3; §4 behaviour bullets → Task 2 (dropdowns, new supplier, mirror removal, reads, legacy helper), Task 4 (kind guard), Task 5 (PDF, link, currency); §5 tests → Tasks 1–5; §6 docs → Task 6. §4 "GRN PDF keeps name only": no change needed.
- Spec §4 says the PDF prints "postcode + city"; `contacts` has no `city` column (address is free text + postcode + country). Task 5 prints address lines, postcode, country. Ledger this as a ruling.
- Type consistency: `SUPPLIER_ID_EXPR`, `FK_SWAP`, `assert_no_dangling`, `map_suppliers_to_contacts` defined in Task 2 and consumed verbatim in Task 3; `supplier_references` defined and consumed in Task 4; `rebuild_table(conn, table, *, sql_replacements, column_exprs)` consistent across Tasks 1–3.
- Review Focus 1–3 pinned by `_seed` in Task 2 (NULL offer id 4, "Dupe Co"/"DUPE CO" lowest id 105, customer "Digi-Key" 104 never chosen); 4 pinned by the crafted POST test in Task 4; 5 pinned by `po_bare` in Task 5.
