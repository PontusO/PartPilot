# Single supplier store — design

**Status:** approved 2026-10-16 (sections 1–3 in chat), implementation pre-approved.
**Branch:** `single-supplier-store`. **Supersedes:** the mirror-on-save bridge of 2026-07-10.

## 1. Problem and backstory

PartPilot has two supplier stores. miniMRP had one (`tblsupaddresses`); on 2026-06-11 the catalog
module loaded it into a thin procurement table `suppliers` (name, short name, URL, currency,
`minimrp_id`) for `part_suppliers`, `purchase_orders` and `goods_receipts` to key on, while the
contacts module loaded the same rows into `contacts` (kind `supplier`) with the full address. On
2026-07-10 a mirror-on-save (`catalog.repo.upsert_supplier`, called from the contacts router) plus a
one-off backfill (contacts migration v4) bridged them. The link is by name, flows one way, and the
resolver/importers create `suppliers` rows that never get a contact (Digi-Key, Mouser, JLCPCB today).

Decision: **Contacts is the only supplier record.** The `suppliers` table is dropped and every
foreign key that pointed at it points at `contacts(id)`.

Live data at decision time: 24 `suppliers` rows, 21 supplier contacts; 18 pairs share a miniMRP id,
3 more match by name, 3 suppliers have no contact.

## 2. Data model

| Table | Column | Before | After |
|---|---|---|---|
| `part_suppliers` | `supplier_id` | `REFERENCES suppliers(id)` | `REFERENCES contacts(id)` |
| `purchase_orders` | `supplier_id` | `REFERENCES suppliers(id)` | `REFERENCES contacts(id)` |
| `goods_receipts` | `supplier_id` | `REFERENCES suppliers(id)` | `REFERENCES contacts(id)` |
| `suppliers` | — | table | **dropped** |

`contacts` is unchanged. A supplier is `contacts.kind = 'supplier'`. `part_supplier_tiers` keys on
`part_suppliers(id)` and is untouched. All other columns, indexes and the stored ids of offers,
POs and GRNs are preserved; only the *value* of `supplier_id` changes (old supplier id → contact id).

## 3. Migration

### 3.1 Core: callable migrations and pre-migration backup

`core.registry.Migration.sql` becomes `str | Callable[[sqlite3.Connection], None]`. The runner
(`core.db.Database.apply_migrations`) calls the callable with the connection instead of
`executescript`. Existing SQL migrations are unaffected.

Before applying **any** pending migration, the runner backs the database up:
`<data_dir>/backups/partpilot-<YYYYMMDD-HHMMSS>.db` via `sqlite3.Connection.backup()` (WAL-safe).
It logs the path. If the backup cannot be written, startup aborts with a clear error and no
migration runs. Skipped when the database file does not exist yet or has no user tables (fresh
install), and for in-memory databases. The backups folder is never pruned automatically.
`Database` learns the data dir (constructor argument `backup_dir`; `create_app` passes
`<data_dir>/backups`; tests may pass `None` to disable).

### 3.2 Catalog migration v23 (callable): `part_suppliers` → contacts

Runs first in feature order. Steps:

1. If a table named `contacts` does **not** exist (fresh database), rebuild `part_suppliers` with
   the new FK (no rows) and return.
2. `ALTER TABLE suppliers ADD COLUMN contact_id INTEGER` (if absent).
3. Map: for each supplier row, `contact_id` = the supplier-kind contact with the same
   `minimrp_id`, else the supplier-kind contact with `lower(trim(name))` equal, else **create** a
   supplier contact (`kind='supplier'`, name, short_name, website ← url, currency,
   `source='sup-merge'`) and use its id. Every supplier row ends with a `contact_id`.
4. Rebuild `part_suppliers` (see 3.4) copying rows with `supplier_id` replaced by the mapped
   `contact_id` (NULL stays NULL).

### 3.3 Purchase-orders migration v6 (callable): POs, GRNs, drop `suppliers`

Runs after contacts in feature order. Steps:

1. If `suppliers` lacks `contact_id` (fresh database: catalog v23 took the early return), rebuild
   `purchase_orders` and `goods_receipts` with the new FK (no rows), drop `suppliers`, return.
2. Rebuild `purchase_orders` and `goods_receipts` (3.4) mapping `supplier_id` through
   `suppliers.contact_id`.
3. Assert every non-NULL `supplier_id` in the three tables resolves to a contact; otherwise raise
   (the transaction is rolled back and startup fails).
4. `DROP TABLE suppliers`.

### 3.4 Table rebuild recipe (shared helper `core.db.rebuild_table`)

SQLite cannot alter a foreign key in place. For a table *T*:

1. `PRAGMA foreign_keys = OFF` (after committing any open transaction; the pragma is a no-op
   inside one). Dropping *T* with enforcement on would cascade-delete `part_supplier_tiers`.
2. Read *T*'s `CREATE TABLE` SQL and its index SQL from `sqlite_master`; textually replace
   `REFERENCES suppliers(id)` with `REFERENCES contacts(id)`; create `T__new` from it.
3. `INSERT INTO T__new SELECT … FROM T` with `supplier_id` replaced by the mapping expression.
4. Verify `COUNT(*)` equal; `DROP TABLE T`; `ALTER TABLE T__new RENAME TO T`; recreate indexes.
   (With `legacy_alter_table` off, the rename rewrites references *to the renamed table* only, so
   child tables' textual `REFERENCES T(id)` keep resolving.)
5. `PRAGMA foreign_key_check` on *T* must return no rows; `PRAGMA foreign_keys = ON`.

### 3.5 Shipped migrations

None are edited. Contacts v4 (backfill into `suppliers`) still runs on fresh databases between
catalog v23 and purchase-orders v6, while `suppliers` still exists; its rows are dropped with the
table. Feature order in `app.py` is unchanged.

## 4. Behaviour

- **Supplier dropdowns** (part form, PO form) list `contacts WHERE kind='supplier'` ordered by
  name. `catalog.repo.suppliers()` and `purchase_orders.repo.suppliers()` read contacts.
- **"+ New supplier…"** on the part form creates a supplier contact with the name only.
  `catalog.repo._get_or_create_supplier(conn, name)` finds a supplier contact by case-insensitive
  trimmed name or inserts one (`source='ui'`). The resolver (`import_bom`, `import_parts`) and
  `part_cleanup` use the same helper, so "Digi-Key"/"Mouser" become contacts once.
- **Mirror removed:** `catalog.repo.upsert_supplier` and `contacts.router._mirror_supplier` are
  deleted.
- **Kind guard:** `contacts.repo.update_contact` refuses changing `kind` away from `supplier` while
  the contact is referenced by any `part_suppliers`, `purchase_orders` or `goods_receipts` row.
  Message: "<name> is used as a supplier on N part(s), M purchase order(s) and K goods receipt(s) —
  it must stay a supplier." Shown on the edit form (`.err`). There is no contact delete.
- **Reads** that joined `suppliers` for a name join `contacts`: PO list/detail/suggestions, GRN
  list/detail, customer-orders cancelled-order warning, part detail/list, Setup part-number
  cleanup, `/catalog/supplier?name=`. The contacts list's "Parts ↗" pill keeps linking by name.
- **PO PDF** supplier block prints name, address, postcode + city, country, "Org no", email and
  phone from the contact (blank lines omitted). **PO detail** links the supplier name to
  `/contacts/<id>`. The GRN PDF keeps name only.
- **PO currency default:** the new-PO form pre-fills `currency` from the chosen supplier contact's
  currency when the field is blank (server side on create).
- **Legacy seeding helper** `catalog.importer.import_tables(suppliers=…)` upserts its rows as
  supplier contacts keyed `(source='sup', minimrp_id)`, exactly like `contacts.importer`, and maps
  `SupplierID` through those contact ids for `part_suppliers`. Test-only.
- **Docs:** README (Parts, Contacts, Integrations/Architecture mention), CLAUDE.md (remove the
  two-stores note if present; mention callable migrations + backup), `docs/design-rules.md` log,
  `docs/user-manual.md` (Contacts → Suppliers; Buying → Raising a PO; Setup → nothing new), memory
  note `two-supplier-stores` rewritten.

## 5. Testing

Populated-database migration tests build a v22/v5 schema (slice `MIGRATIONS` lists), seed, migrate,
assert:

1. Two suppliers linked by miniMRP id, one by name only (different case/whitespace), one orphan →
   every offer/PO/GRN points at the right contact; orphan gets a new supplier contact with name,
   website, currency; cost-tier count unchanged; `suppliers` gone; indexes present.
2. Fresh database migrates from nothing; `sqlite_master` shows the three tables referencing
   `contacts(id)`; `suppliers` absent.
3. Runner backup: file created in `backups/` when migrations are pending; not created when none;
   startup raises when the backup directory is unwritable.
4. Dangling reference (offer whose `supplier_id` matches no supplier row) → migration raises, the
   database is unchanged (transaction rolled back).

Behaviour tests: "+ New supplier…" creates a supplier contact; resolver import creates one Digi-Key
contact and reuses it on a second import; kind change refused with counts / allowed when
unreferenced; PO PDF text contains the address lines; PO form pre-fills currency. Existing tests
inserting into `suppliers` (tests/test_contacts.py, test_purchase_orders.py, test_web.py) are
rewritten to create contacts.

## 6. Rollout

Branch `single-supplier-store` in worktree `/home/service1/partpilot-suppliers`. Full suite green,
fresh-context review, scratch instance on 8010 against a copy of the live database for user checks
(supplier dropdowns, PO PDF). Merge on request; restart the service; the first production start
writes `data/backups/partpilot-<ts>.db` and applies catalog v23 + purchase-orders v6.

🟥 Behaviour change: suppliers are managed only in Contacts; `suppliers` is dropped; the runner
backs the database up before any migration.
