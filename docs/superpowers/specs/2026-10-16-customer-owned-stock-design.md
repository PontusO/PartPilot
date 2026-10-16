# Customer-owned stock ("shelves") — design

**Date:** 2026-10-16
**Status:** approved in conversation (sections 1–8), written up for implementation
**Supersedes:** the "Ignore our own stock" checkbox on Setup → Import parts (commit 963678b), which
is removed by this work.

## Purpose

We often buy parts for a specific customer who will not use our normally stocked items. Today
PartPilot has one pool of stock per part, so the same resistor bought for two customers collides
with each other and with our own stock: the resolver says "in stock", the work order issues from
the shared pool, and nothing records whose parts they were.

Ownership of stock becomes a property of **where the stock sits**, never of the part. A **shelf**
is a `stock_locations` row with a kind (`ours` or `customer`) and, for a customer shelf, an owner.
One catalog part per manufacturer part number is kept; a part can sit on several shelves at once.

## What the user said vs. what is assumed

Said (brainstorm 2026-10-15/16):

- A shelf belongs to a **customer** or to **one customer order** (a job); chosen per shelf.
- Customer-shelf parts are **the customer's property, paid up front**. They are excluded from our
  stock valuation and from the build's material cost; we charge labour/overhead only.
- **Leftovers stay on the shelf.** Nothing moves automatically.
- A work order draws from **exactly one shelf**. A shortage on a customer shelf is a shortage to
  buy for that shelf even if Main has the part. No fallback to Main.
- Approach A chosen: shelves are stock locations with an owner (not a separate owner dimension,
  not a kit system outside stock).

Assumed:

- The catalog keeps one part per MPN; the importers keep linking rows to existing parts by MPN.
- "Main" stays our stock. More than one shelf of our own is allowed (kind `ours`), so our stock
  is "all `ours` shelves", not one row.
- There is no contact-delete route in Contacts today, so nothing needs blocking there.
- Work orders cannot be edited after creation today (only rescheduled / actioned); a shelf is fixed
  when the work order is created. To change it, cancel the allocated work order and recreate it.
  (Section 4 of the chat design said "through the existing edit path" — there is none; this is the
  correction.)

## Non-goals

- Moving stock between shelves (a TRANSFER movement). Corrections are two adjustments. Revisit if
  routine.
- Customer-owned parts physically mixed into our bins (needs an owner dimension — approach B).
- Per-BOM-line "customer supplied / ours" marking.
- Invoicing the customer for handling, or any pricing layer (pricing stays outside PartPilot).
- The CLI resolver (no database → never checked stock; unchanged).

## Architecture

### Data model (all additive migrations)

**catalog, migration 22 — "stock shelves"**

```sql
ALTER TABLE stock_locations ADD COLUMN kind TEXT NOT NULL DEFAULT 'ours';   -- 'ours' | 'customer'
ALTER TABLE stock_locations ADD COLUMN owner_contact_id INTEGER;             -- contacts.id (customer); soft join
ALTER TABLE stock_locations ADD COLUMN customer_order_id INTEGER;            -- customer_orders.id; soft join; NULL = per-customer shelf
ALTER TABLE stock_locations ADD COLUMN created_at TEXT NOT NULL DEFAULT (datetime('now'));
ALTER TABLE stock_locations ADD COLUMN retired_at TEXT;                      -- hidden from pickers, kept for history
CREATE UNIQUE INDEX ux_stockloc_name ON stock_locations(name);
ALTER TABLE parts ADD COLUMN customer_qty REAL NOT NULL DEFAULT 0;           -- on-hand summed over customer shelves
-- Safety net: every stock row belongs to a shelf. (Live DB: no null rows.)
UPDATE part_stock SET location_id = (SELECT MIN(id) FROM stock_locations) WHERE location_id IS NULL;
UPDATE stock_movements SET location_id = (SELECT MIN(id) FROM stock_locations) WHERE location_id IS NULL;
```

`owner_contact_id` / `customer_order_id` are plain integers: catalog migrates before Contacts and
Customer Orders, so no FK is declared (repo rule: cross-feature ties are soft joins). The repo
checks they point at a real customer / order.

**purchase_orders, migration 5 — "receive-into shelf"**

```sql
ALTER TABLE purchase_orders ADD COLUMN location_id INTEGER REFERENCES stock_locations(id);
UPDATE purchase_orders SET location_id = (SELECT MIN(id) FROM stock_locations WHERE kind = 'ours')
WHERE location_id IS NULL;
```

**customer_orders, migration 7 — "allocation shelf"**

```sql
ALTER TABLE allocations ADD COLUMN location_id INTEGER REFERENCES stock_locations(id);
UPDATE allocations SET location_id = (SELECT MIN(id) FROM stock_locations WHERE kind = 'ours')
WHERE location_id IS NULL;
```

**work_orders**: `location_id` already exists. Repo treats NULL as the default own shelf and new
work orders always get one. (No migration.)

**Semantics of the part totals (behaviour change).** `parts.total_qty` and `parts.total_alloc`
sum **only `ours` shelves**. `parts.customer_qty` sums customer shelves. Every existing consumer
(catalog list "free", below-minimum, shortage analyser, resolver, webshop sync pushing
`total_qty` as shop stock) therefore treats customer stock as "not ours" without being touched.
Figures are unchanged at rollout because no customer shelf exists yet.

### Stock core (`web/features/catalog/stock.py`)

- `post_movement(conn, part_id, *, delta, mtype, reference=None, note=None, user=None, location_id)`
  — `location_id` is **required** (an `int`). The row for `(part_id, location_id)` is created if
  missing. **The fallback to "any stock row for this part" is removed**: a movement never touches
  another shelf. The ledger row's `qty_after` becomes **that shelf's on-hand after the move**
  (identical to today's part total while only Main exists; a customer shelf's ledger then reads as
  its own running balance). Then `_rollup(conn, part_id)`:

  ```sql
  UPDATE parts SET
    total_qty    = (SELECT COALESCE(SUM(pk.on_hand),0) FROM part_stock pk JOIN stock_locations l ON l.id=pk.location_id WHERE pk.part_id=? AND l.kind='ours'),
    customer_qty = (SELECT COALESCE(SUM(pk.on_hand),0) FROM part_stock pk JOIN stock_locations l ON l.id=pk.location_id WHERE pk.part_id=? AND l.kind='customer')
  WHERE id=?;
  ```

- `adjust_stock(db, part_id, *, delta, mtype, reference, note, user, location_id)` — `location_id`
  required too; callers pass the shelf explicitly (the part page's stock form carries the shelf of
  the row being adjusted).
- `recompute_alloc(conn, part_id)` — **moved here** from the duplicated `_recompute_part_alloc`
  in `customer_orders/repo.py` and `despatch/repo.py` (both deleted, both call this). It sets
  `part_stock.allocated` per shelf from `allocations` grouped by `location_id`, and
  `parts.total_alloc` = allocations on `ours` shelves.
- `free_on_shelf(conn, part_id, location_id) -> float` = on_hand − allocated for that row (0 if no
  row). Used by work orders, shortages, allocation.

### Shelf repo (`web/features/catalog/repo.py`)

- `locations(db, *, include_retired=False, kind=None, owner_contact_id=None) -> list[dict]` —
  rows `{id, name, kind, owner_contact_id, owner_name, customer_order_id, order_ref, retired_at,
  part_count, on_hand_total}`. `owner_name` / `order_ref` via LEFT JOIN on `contacts` /
  `customer_orders` (soft). Replaces today's `locations(db)`; all existing callers get the default
  (active shelves, all kinds).
- `default_shelf_id(db) -> int` — the lowest-id active `ours` shelf, creating "Main" if none
  exists. Replaces `_location_id(conn, None)` semantics; `_location_id` is deleted and every caller
  passes an explicit id (forms default to `default_shelf_id`).
- `pickable_shelves(db, *, customer_id: int | None = None) -> list[dict]` — active `ours` shelves
  plus, when `customer_id` is given, that customer's active shelves (job shelves first, then the
  per-customer shelf). Used by every shelf picker.
- `shelves_for_customer(db, contact_id)`, `shelf_for_order(db, order_id) -> dict | None`.
- `preferred_shelf_id(db, *, customer_id, order_id) -> int` — job shelf → customer shelf →
  `default_shelf_id`.
- `create_shelf(db, *, kind, owner_contact_id=None, customer_order_id=None, name=None) -> int`
  — validates: `kind in ('ours','customer')`; customer shelf needs an owner that is a contact of
  kind `customer`; `customer_order_id` must belong to that owner; auto-name
  `"{owner short_name or name}"` or `"{owner} · {order_ref}"` when `name` is empty; name unique
  (ValueError "A shelf called … already exists.").
- `rename_shelf(db, id, name)`, `retire_shelf(db, id)` — retire refuses (ValueError) when any
  `part_stock.on_hand != 0` on it, any PO with `status IN ('draft','ordered')` receives into it, or
  any WO with `status IN ('allocated','issued')` uses it. `Main`/the last active `ours` shelf cannot
  be retired.
- `shelf_stock(db, id) -> list[dict]` — per part: part_no, value, on_hand, allocated, free,
  unit_cost, line value. `shelf_movements(db, id, limit=100)`.
- `get_part` stock rows gain `kind` and `owner_name` per row; `part["stock_ours"]`,
  `part["stock_customer"]` totals.

### Shelf pages and management (catalog feature routes)

| Method | Path | Does |
|---|---|---|
| GET | `/catalog/shelves` | All shelves (ours + customer), kind badge, owner, order, part count, on-hand. Linked from Setup & Tools tile **"Stock shelves"** (admin) and from the part page's stock panel. Admin: "New own shelf" form (name). |
| GET | `/catalog/shelves/{id}` | One shelf: header (owner, order, kind), table of parts with on-hand / allocated / free / value, recent ledger. Row button "Adjust" uses the part's existing stock form pre-set to this shelf. Admin: rename, retire (buttons; retire refusal shown as `.err`). |
| POST | `/catalog/shelves` | Create. Fields `kind`, `owner_contact_id`, `customer_order_id`, `name`, `return_to`. 303 → `return_to` (contact / order page) or the new shelf page. Errors re-render the origin page with `.err`. |
| POST | `/catalog/shelves/{id}/rename` | Admin. |
| POST | `/catalog/shelves/{id}/retire` | Admin. |

Roles: list/detail for all logged-in users; create for `PURCHASE_ROLES`; rename/retire admin.

**Contact page** (kind `customer`): panel **Stock shelves** listing the customer's shelves (name,
job order if any, parts, on-hand, link) and a one-field "New shelf" form (optional name) posting to
`/catalog/shelves` with `kind=customer`, `owner_contact_id`, `return_to`.

**Customer order page**: a **Job shelf** line: either a link to the order's shelf or a "Create job
shelf" button posting to `/catalog/shelves` with `customer_order_id`.

### Purchasing into a shelf (`purchase_orders`)

- `create_po(db, data)` takes `location_id` (default `default_shelf_id`). PO form: shelf picker
  listing all active shelves (a PO has no customer of its own), own shelves first.
  PO detail header shows the shelf; `export.py` prints "Deliver to shelf: <name>" on the PDF and a
  `shelf` column in the CSV.
- `receive_po(...)` posts every RECEIVE with `location_id=po["location_id"]`; refuses
  (ValueError) if that shelf is retired. The GRN header shows the shelf (join through the PO).
- `shortage_suggestions(db)` becomes **per (part, shelf)**: demand from allocated WOs grouped by
  `(wl.part_id, COALESCE(w.location_id, default))`; free = `free_on_shelf`; on_order = open PO
  lines whose PO receives into the same shelf. Rows gain `location_id`, `location_name`,
  `location_kind`.
- `shortage_suggestions_grouped(db)` groups by shelf, then supplier:
  `[{location_id, location_name, suppliers:[{supplier_id, supplier_name, lines}]}]`.
- `create_pos_from_suggestions(db, selections: dict[tuple[int,int], float], user)` — key is
  `(part_id, location_id)`; one draft PO per `(supplier, shelf)`. Form checkbox values are
  `"{part_id}:{location_id}"`; qty fields `qty_{part_id}_{location_id}`.
- Last purchase price + cost-tier refresh on receipt are unchanged (who paid does not change the
  market price).

### Work orders draw from one shelf (`work_orders`)

- `create_work_order(db, data)`: `location_id` required; router defaults to `default_shelf_id`.
  New-WO form: shelf picker = `pickable_shelves(db)` (all active, ours first).
- `build_from_order` / `create_work_orders_for_order(db, order_id, selections, *, location_id,
  ...)`: picker = `pickable_shelves(db, customer_id=order.customer_id)`, pre-selected
  `preferred_shelf_id`. If the customer has no shelf, a `.note` under the picker says so with a
  "Create customer shelf" button (POST `/catalog/shelves`, `return_to` = this page).
- `get_work_order`: each line gets `shelf_on_hand`, `shelf_allocated`, `available` (=
  `free_on_shelf` at the WO's shelf); `short = max(0, qty_required − available)`. Header gets
  `shelf` `{id, name, kind, owner_name}` and `customer_supplied = kind == 'customer'`.
- Cost: when `customer_supplied`, `wo["loaded_build_cost_charged"] = 0.0` and
  `wo["loaded_build_cost_reference"] = <today's figure>`; template shows "Material:
  customer-supplied (reference value X)". Own-shelf WOs unchanged.
- `issue_work_order`: ISSUE with `location_id=wo["location_id"]` (already) — strict now that the
  core has no fallback. Negative on-hand allowed (as today).
- `finish_work_order`: BUILD onto the WO shelf (already).
- `_buy_by_lines`, planning calendar: use per-shelf `available`.
- `cancel_work_order`: unchanged. No shelf edit (see assumptions).

### Customer order fulfilment, allocation, despatch

- `fulfilment_proposals`: `free` = sum of `free_on_shelf` over
  `candidate_shelves(order) = [job shelf, customer shelf, *ours shelves]` (deduped, active).
- `allocate_order`: walks the same candidate shelves in order, taking `min(need, free_on_shelf)`
  from each and inserting one `allocations` row per shelf taken (`location_id` set); calls
  `stock.recompute_alloc`.
- `release` / `_consume_allocation`: unchanged logic, call `stock.recompute_alloc`.
- `despatch.dispatch`: ISSUE per allocation's `location_id` for the allocated part of each line;
  any unallocated remainder issues from `default_shelf_id`. Packing list / despatch note print the
  shelf when it is a customer shelf ("Shipped from customer shelf: <name>").
- `despatch.shippable_lines`: free counted over the order's candidate shelves.

### Resolver and the three import screens

- `catalog/stock_index.py`: `build_stock_index(db, location_ids: list[int]) -> StockIndex` —
  on-hand/allocated from `part_stock` rows on those shelves only (released versions, documents
  excluded, as today).
- `purchasing/service.py`: `resolve_bom_file(..., check_stock: bool = True,
  stock_location_id: int | None = None, ...)`. With `check_stock` and a database: index over
  `[stock_location_id]` if given, else over all active `ours` shelves. `ResolvedRun` gains
  `stock_shelf: str | None` (shelf name, or "our shelves", or None when not checked). After
  resolution the service rewrites IN_STOCK notes `"in stock: …"` → `"in stock on {shelf}: …"` so
  the engine (`pipeline.py`) stays shelf-agnostic. `run_purchase` passes the same through.
- **One control on all three upload pages** — purchasing `upload.html`, setup
  `import_parts_upload.html`, assemblies `assembly_import.html`: `<select name="stock_shelf">`
  with options: each active `ours` shelf (Main pre-selected), each active customer shelf (labelled
  "Customer: <name>"), and `none` = "Don't check stock". The purchasing `check_stock` checkbox and
  the setup `ignore_stock` checkbox are **removed**. Routers map `none` → `check_stock=False`,
  otherwise `stock_location_id=int(value)` (invalid/retired id → 400).
- Setup import meta stores `stock_location_id`; the map, review and result pages show
  "Stock checked on **<shelf>**" / "Stock not checked" instead of the current notes. Purchasing
  `result.html` likewise. Assembly import review shows it in its header line.
- Import review rows linked to an existing catalog part show the shelf's free quantity
  (`plan[i]["shelf_free"]`, from `free_on_shelf` on the chosen shelf; None when not checked), so
  "in catalog but not on this shelf" is visible.
- `build_import_plan(db, run)` — unchanged linking by MPN; adds `shelf_free` as above (needs the
  shelf id: `build_import_plan(db, run, stock_location_id=None)`).

### Costing, valuation, reports

- Part page stock panel: one row per shelf with kind badge (`.badge stock` ours, `.badge warn`
  customer + owner), on-hand / allocated / free; totals line "Ours: N · Customer-owned: M". The
  stock-move form's **adjust** target applies to the selected shelf row's on-hand (today it uses
  the part total — a latent bug once a part has two shelves).
- Catalog list: unchanged (shows our free); below-minimum unchanged (our figures).
- Reports → stock movements: `location_id` filter and a "Shelf" column; the summary groups by
  shelf when filtered to none.
- Shelf page shows a **value** line (Σ on_hand × unit_cost) — what the customer has on our premises.
- Webshop sync: unchanged (reads `total_qty`, now ours-only).

### Data flow (customer job, end to end)

1. Customer order CO-00042 for Acme → "Create job shelf" → shelf "Acme · CO-00042".
2. Setup → Import parts (or Purchasing): upload BOM, **Check stock on shelf = Acme · CO-00042** →
   every row goes to Digi-Key/Mouser (the shelf is empty), parts link/create in the catalog.
3. Work order from the order → shelf pre-set to the job shelf → every line short.
4. Purchase orders → suggestions grouped under the job shelf → one PO per supplier, receive-into
   the job shelf. Goods receipt posts onto the shelf; part page shows "Ours: 3000 · Customer-owned:
   100".
5. Issue WO → ISSUE from the job shelf only. Finish → boards BUILD onto the job shelf.
6. Allocate order → takes boards from the job shelf → despatch issues from it.
7. Leftover resistors stay on the shelf, visible on the shelf page, excluded from our valuation.

## Error handling

| Situation | Behaviour |
|---|---|
| Picker posts a retired / unknown / other customer's shelf id | repo `ValueError` → 400 re-render with `.err`; nothing written |
| Create customer shelf without owner, owner not a customer, order not the owner's | `ValueError` with a plain message |
| Duplicate shelf name | `ValueError` "A shelf called … already exists." |
| Retire with stock / open PO / open WO / last own shelf | `ValueError` naming the blocker |
| Receive into a retired shelf | `ValueError` (belt and braces; retire already refuses with open POs) |
| Issue leaves a customer shelf negative | allowed; shelf page and part page show `.badge bad` |
| WO from an order whose customer has no shelf | defaults to Main; hint + "Create customer shelf" button |
| Order cancelled with stock on its job shelf | shelf stays, owner keeps it; order link kept |
| Upload page `stock_shelf` invalid | 400 with the upload page re-rendered and `.err` |

## Testing

All with `create_app` + temp DB + `TestClient`; resolution via `FakeSearcher`/monkeypatched
`resolve_bom_file`; no network.

- **Stock core:** movement on a shelf without a row creates it and leaves the other shelf
  untouched (pins fallback removal); rollup `total_qty` ours-only / `customer_qty` customer-only
  after RECEIVE, ISSUE, ADJUST; `recompute_alloc` per shelf; migration backfill of a null-location
  row.
- **Shelves:** create per-customer and per-job from contact/order; auto-name; duplicate name
  refused; owner must be a customer; retire refused with stock / open PO / allocated WO / last own
  shelf, allowed when clean; retired absent from `pickable_shelves`; `preferred_shelf_id` order.
- **Purchasing:** receive lands on the PO's shelf, not Main; last price still recorded; shortage
  per shelf (customer WO short while Main has plenty; open PO into Main does not cover a customer
  shelf); suggestions grouped shelf × supplier create one PO per pair pointed at its shelf;
  receiving into a retired shelf refused.
- **Work orders:** from-order picker offers own + that customer's shelves only, prefers job shelf;
  issue posts only on the WO shelf; finish lands on the WO shelf; availability per shelf on the
  page; customer-shelf WO shows customer-supplied material with zero charged.
- **Fulfilment / allocation / despatch:** proposals count free from the customer's and own shelves,
  not another customer's; allocation rows carry the shelf in preference order; despatch issues from
  the allocation's shelf.
- **Resolver / imports:** index from the chosen shelf only (same part in Main, absent on the
  customer shelf → goes to the distributor); `none` skips the index; all three upload pages render
  the selector and pass it through; notes name the shelf; import review shows `shelf_free` for a
  linked row; `ignore_stock` / `check_stock` fields no longer exist.
- **Reports:** movement report filters by shelf.
- **Part page:** per-shelf rows and totals; adjust targets the chosen shelf.

## Files

- `web/features/catalog/migrations.py` (v22), `stock.py`, `stock_index.py`, `repo.py`,
  `router.py`, templates `shelves.html`, `shelf.html`, `part.html` (stock panel), partial
  `_shelf_select.html` (shared picker macro).
- `web/features/contacts/router.py` + `contact.html` (shelves panel).
- `web/features/customer_orders/migrations.py` (v7), `repo.py` (allocation, proposals),
  `router.py` + order template (job shelf line).
- `web/features/purchase_orders/migrations.py` (v5), `repo.py`, `router.py`, `export.py`,
  templates (form, detail, suggestions).
- `web/features/work_orders/repo.py`, `router.py`, templates (form, from-order, detail).
- `web/features/despatch/repo.py`, templates/PDF.
- `web/features/purchasing/service.py`, `router.py`, `upload.html`, `result.html`.
- `web/features/assemblies/router.py`, `import_bom.py`, `assembly_import.html`,
  `assembly_import_review.html`.
- `web/features/setup/router.py`, `import_parts_*.html`, TOOLS tile "Stock shelves".
- `web/features/reports/repo.py`, `router.py`, template.
- `web/features/catalog/import_parts.py` (`shelf_free`).
- Docs: README (Parts → shelves; Purchasing; Work orders; Setup & Tools), `docs/design-rules.md`
  change log (shelf badge colours, picker), this spec.

## Behaviour change callouts (for the terminal banner at delivery)

1. `parts.total_qty` / `total_alloc` exclude customer shelves; `customer_qty` added. No change in
   figures until a customer shelf holds stock.
2. Stock movements never fall back to another shelf's row. A work order on a shelf that lacks a
   part goes negative on that shelf instead of silently consuming Main.
3. Shortage analysis and suggested POs are per shelf; one PO per supplier per shelf.
4. The purchasing "check stock" and import "ignore stock" checkboxes are replaced by a shelf
   selector on all three upload pages.
5. Allocations and despatch are shelf-aware (allocation rows record a shelf).
