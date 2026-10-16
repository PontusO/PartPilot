# Part lifecycle: retire, then delete — design

**Date:** 2026-10-16
**Status:** approved in conversation (sections 1–3), implementation pre-approved
**Branch:** `part-lifecycle`

## Problem

Nothing in PartPilot removes a part of any kind. An assembly created by mistake, or a product that
has reached end of life, stays in the Assemblies list and in every picker forever. The only exit
today is "Convert to component", which flips `parts.kind` and nothing else. The live database holds
71 assemblies: 49 with history (work orders, order lines or stock movements), 16 with a BOM but no
history, and 5 with nothing at all. Components have the same gap.

## Decisions (made by the user)

1. A part must be **retired** before it can be **deleted**. Retire is a mandatory first step.
2. **Delete is admin only.** Retire and unretire are for the roles that edit parts today
   (admin, purchasing).
3. The lifecycle applies to **all parts at once**, components and assemblies, with one mechanism on
   the `parts` table and buttons on both the part page and the assembly page.
4. **Run out, don't restart.** A retired assembly cannot get new work orders, but existing stock
   can still be despatched. A retired component in a live BOM still builds and is consumed, but
   leaves purchase suggestions and the resolver's own-stock check so nobody buys more.

## Section 1 — Data model and lifecycle rules

One new column, added by catalog migration v24:

```sql
ALTER TABLE parts ADD COLUMN retired_at TEXT;        -- NULL = active
CREATE INDEX ix_parts_retired ON parts(retired_at);
```

Retiring is a **part-level operation owned by the catalog feature**, applied to the **whole version
group** (`COALESCE(version_group_id, id)`): retiring any version stamps every version with the same
timestamp; unretiring clears them all; deleting removes them all. Draft versions are not special.

States and transitions:

| From → To | Who | Precondition | Effect |
|---|---|---|---|
| Active → Retired | admin, purchasing | none | `retired_at = datetime('now')` on every version in the group. Stock, BOM, offers, documents, article number untouched. |
| Retired → Active ("Unretire") | admin, purchasing | none | `retired_at = NULL` on every version in the group. |
| Retired → Deleted | admin | retired **and** no blockers (below) | `DELETE FROM parts` for every version in the group. Existing `ON DELETE CASCADE` removes their own `bom_lines` (as parent), `part_suppliers` (+ tiers), `part_stock`, `part_price_tiers`. |

**Delete blockers** — any non-zero count refuses the delete. Each is checked across every version
in the group, and only when the owning feature's table exists (partial test registries):

| Label shown | Table.column |
|---|---|
| work orders | `work_orders.assembly_id` |
| work order lines | `work_order_lines.part_id` |
| customer order lines | `customer_order_lines.part_id` |
| allocations | `allocations.part_id` |
| despatch lines | `despatch_lines.part_id` |
| purchase order lines | `purchase_order_lines.part_id` |
| goods receipt lines | `goods_receipt_lines.part_id` |
| stock movements | `stock_movements.part_id` |
| assemblies using it | `bom_lines.child_id` |
| devmgmt variants | `variants.assembly_id` |

`stock_movements` cascades on delete in the schema, but it is a ledger, so it is a blocker rather
than silently erased. Refusal text: `Cannot delete <part_no>: 3 work orders, 12 stock movements,
used in 2 assemblies.` Only non-zero items are listed. Deleting a part that is not retired is
refused with `Retire <part_no> before deleting it.`

Unchanged: lookups by id (`get_part`, `get_assembly`, order and work-order pages) still resolve
retired parts so history renders. Lookups by number for **uniqueness** (`find_part_by_part_no`,
article-register and setup "number already used" checks) still see retired parts: a retired number
stays taken. "Convert to component" works on a retired assembly and leaves `retired_at` alone.

## Section 2 — Where retired parts stop appearing

**Lists** (hidden by default, `Show retired` checkbox re-submits the filter):
- Parts list (`catalog.repo.list_parts`, new `include_retired: bool = False`) and Assemblies list
  (`assemblies.repo.list_assemblies`, same parameter). Shown retired rows carry a `badge muted`
  **Retired**. Both `summary()` functions exclude retired parts from every count.
- Article Register keeps showing allocated numbers regardless; the number stays allocated.

**Pickers** gain `AND p.retired_at IS NULL`:
- `work_orders.repo.assemblies` (new work order), `customer_orders.repo.parts_for_picker`,
  `purchase_orders.repo.parts_for_picker`, `assemblies.repo.parts_for_picker` (Add Component).
- Lines that already reference a retired part keep rendering, with a `badge muted` **retired**
  beside the part number on the assembly BOM table and the work-order lines table.

**Server-side guards** (hidden is not enough; a stale form or typed id must fail):
- `work_orders.repo.create_work_order` raises `ValueError("<part_no> is retired and cannot be
  built.")` when the assembly is retired. Building an *active* assembly whose BOM lists a retired
  component is allowed and consumes it.
- `assemblies.repo.add_bom_line` (or the equivalent add path) raises
  `ValueError("<part_no> is retired and cannot be added to a BOM.")`.
- `customer_orders` add-line and `purchase_orders` add-line paths raise the same shape of error
  for a retired part id.

**Buying and resolving:**
- `purchase_orders.repo.shortage_suggestions` skips retired parts (same `continue` as unlimited
  parts), so a short retired component is never suggested.
- `catalog.stock_index.build_stock_index` excludes retired parts, so the resolver's own-stock
  pre-check never reports a retired part as covering a line.
- `catalog.import_parts` (bulk BOM import into the catalog, used by the Purchasing screen and the
  assemblies BOM importer): `find_part_id_by_mpn` gains `include_retired: bool = False`. When the
  only catalog match for a line is retired, the line is **not linked** and is reported with the
  note `matches retired part <part_no>`; it is created as a new part only if the operator accepts
  it like any other new line. The planner never silently lands on a retired part.
- WooCommerce sync: `_reconcile_one` skips a product whose matching part is retired, with the
  report note `part is retired`. It does not create a duplicate.

**Still works unchanged:** despatching existing stock on an order line that already names a
retired part; stock adjustments on the part page; old orders, GRNs and POs; where-used; documents;
PDF and xlsx exports; versions panel.

## Section 3 — UI, code, tests, docs

### UI (per `docs/design-rules.md`)

- **Part page** and **assembly page** action bars: active part → **Retire** button (admin,
  purchasing) with `confirm()`. Retired part → **Unretire** (admin, purchasing) and, for admins
  only, **Delete** with `confirm()`. All are real `<button>`s in `<form method="post">`.
- A retired part's `<h1>` shows `badge muted` **Retired <date>**.
- Refusals render in the page's `.err` box (the assembly page already has one; the part page gains
  the same `{% if error %}<div class="err">…` slot and a `_render_detail`-style helper).
- Lists: `Show retired` checkbox next to the existing filters, `onchange="this.form.submit()"`,
  preserved across pagination links on the parts list.

### Code

- New `catalog/lifecycle.py`:
  - `group_ids(conn, part_id) -> list[int]` — every version id in the part's group.
  - `delete_blockers(conn, part_id) -> dict[str, int]` — label → count, only existing tables.
  - `retire_part(db, part_id) -> None`, `unretire_part(db, part_id) -> None`.
  - `delete_part(db, part_id) -> None` — raises `ValueError` when not retired or blocked.
  - `format_blockers(part_no, blockers) -> str` — the refusal sentence.
- Catalog migration v24 (`retired_at` + index). Additive; no data change.
- Routes: catalog `POST /catalog/{id}/retire`, `/unretire`, `/delete`; assemblies
  `POST /assemblies/{id}/retire`, `/unretire`, `/delete` calling the same lifecycle functions and
  redirecting to the assembly page (delete redirects to the list). Retire/unretire gated by the
  feature's write roles, delete by `frozenset({"admin"})` (403 otherwise).
- `get_part` adds `retired = bool(retired_at)`; `get_assembly` adds the same, and each BOM line
  gets `child_retired`.

### Tests

- `tests/test_lifecycle.py`: retire stamps the whole group; unretire clears it; delete refuses when
  not retired; delete refuses for each blocker type (one test per table, parametrised); clean
  delete removes every version and cascades offers, tiers, stock rows and own BOM lines;
  blockers ignore absent tables.
- Pickers: one test per picker asserting a retired part is absent (`work_orders.assemblies`,
  `customer_orders.parts_for_picker`, `purchase_orders.parts_for_picker`,
  `assemblies.parts_for_picker`).
- Guards: `create_work_order` refuses a retired assembly; `add_bom_line` refuses a retired child.
- `shortage_suggestions` skips a retired part; `build_stock_index` excludes a retired part;
  `find_part_id_by_mpn` ignores retired unless asked.
- Web: `Show retired` toggles list membership; Retire/Unretire by purchasing → 303; Delete by
  purchasing → 403; Delete by admin of an unretired part → 400 with the refusal text; Delete by
  admin of a retired, unreferenced part → 303 to the list and the part is gone.

### Docs

- `docs/user-manual.md`: new **Retiring and deleting a part** subsection under *Parts*, a
  matching **Retiring and deleting an assembly** under *Assemblies*, one sentence in *Buying →
  Purchase suggestions* (retired parts are never suggested) and in *Work orders → Creating a
  stand-alone work order* (retired assemblies cannot be built).
- `docs/design-rules.md` change log: Retire/Unretire/Delete button placement, Retired badge,
  Show retired checkbox convention.
- `README.md`: one line under Parts/Assemblies.
- 🟥 banner in the final message: new lifecycle, retired parts leave pickers and suggestions,
  admin-only delete.

### Rollout

Ordinary merge and restart. The migration is additive; the pre-migration backup runs as usual.
Nothing is retired by the migration — the operator retires parts by hand afterwards.
