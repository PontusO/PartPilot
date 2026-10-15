# Generic parts import from any BOM — design

**Date:** 2026-10-15
**Status:** approved in conversation, awaiting written review
**Lives under:** Setup & Tools (admin only)

## Purpose

Let an admin turn *any* BOM or part list (CSV or Excel, any column layout) into catalog parts.
Today the only importer is tied to one assembly and to the fixed column aliases in
`config/column_mappings.yaml`. This tool adds a user-controlled column-mapping step in front of
the same resolver and review flow, and drops the assembly: it produces catalog parts only.

Success looks like: upload a KiCad, Altium, EAGLE or hand-rolled spreadsheet, confirm (or fix)
the column mapping on one screen, tick the parts to create on a review screen, done. Second
upload of the same layout needs no re-mapping.

## What the user said vs. what is assumed

Said:
- Generic: works for any BOM layout.
- Program detects available fields and asks for a mapping to PartPilot fields.
- Lives as a tool in Setup & Tools.
- Rows are resolver-enriched (own stock, then Digi-Key/Mouser) before parts are created.
- Mappings are auto-remembered by header set; no naming, no management UI.

Assumed (routine calls):
- Admin only, like every Setup tool.
- Excel: first sheet only, as the existing reader does.
- Rows whose MPN already exists in the catalog are linked and reported, never modified.
- Build quantity for the resolver's stock/pricing check defaults to the settings value.

## Non-goals

- Attaching rows to an assembly (the assembly importer keeps doing that).
- Updating existing parts from the file.
- Named mapping profiles or a mapping management screen.
- Mapping to catalog fields the resolver does not understand (manufacturer, supplier P/N,
  cost). The resolver supplies those. Revisit if real files make this painful.

## Architecture

### Shared import engine (refactor)

`web/features/assemblies/import_bom.py` currently holds both the generic part work
(`build_import_plan`, `_create_part`, the status constants) and the assembly-specific work
(adding BOM lines in `apply_import_plan`). Split it:

- **New** `web/features/catalog/import_parts.py` — owns the status constants
  (`IN_INVENTORY`, `NEW`, `UNRESOLVED`, `MANUAL`, `SKIP`), `build_import_plan(db, run)`,
  `create_part_from_item(db, item)`, and a new `apply_parts_plan(db, plan, accepted) -> dict`
  that creates accepted parts and returns `{created, linked, skipped, review, part_ids}` where
  `part_ids` is a per-plan-row list of the catalog part id (or `None`) for the caller to use.
- `assemblies/import_bom.py` keeps `apply_import_plan(db, assembly_id, plan, accepted)` but
  implements it as `apply_parts_plan` followed by the BOM-line loop. Imports of the moved names
  are re-exported there so the assembly router and its tests are untouched.

Resolution logic itself is not touched; both importers still call
`purchasing.service.resolve_bom_file`.

### Resolver entry point: per-run mapping

`resolve_bom_file(...)` gains `mappings: dict | None = None`. When given, it is used instead of
`load_column_mappings(None)`. The Setup tool passes `{field: [exact_header]}` **only for the
fields the user mapped**; unmapped fields are omitted so `detect_columns` cannot fuzzy-match a
column the user chose to ignore. The exact header hits alias step 1 of `detect_columns`.

### Setup tool

Routes (all `require_role(request, {"admin"})`), under the setup router prefix:

| Method | Path | Does |
|---|---|---|
| GET | `/setup/import-parts` | Upload form. |
| POST | `/setup/import-parts` | Save upload to `jobs_dir/pimp-<job>-<name>`, read headers + up to 3 sample rows, compute pre-fill, render mapping screen. |
| POST | `/setup/import-parts/resolve` | Validate mapping, remember it, resolve in threadpool, build plan, save `jobs_dir/piplan-<job>.json`, render review. |
| POST | `/setup/import-parts/apply` | Load plan by job id (hex-guarded), `apply_parts_plan`, delete plan + upload, render result. |

The uploaded file is kept on disk from the mapping POST until apply (the resolver needs it,
and a failed resolve can be retried from the mapping screen without re-uploading). The plan JSON is kept between review
and apply, as the assembly importer does.

Header reading reuses `partpilot.bom.readers.read_table` (header-row detection, delimiter
sniffing, Excel first sheet). Sample values are the first non-empty values per column, max 3.

### Mapping screen

One row per source column: header, the sample values, a `<select>` of targets:
`refdes`, `qty`, `value`, `device` (labelled "Device / MPN"), `package`, `description`,
`comment`, and `ignore` (default). Validation on submit:
- each target used at most once (server-side; the browser select cannot enforce it);
- at least one of `value` or `device` mapped;
otherwise re-render the mapping screen with the error and the user's choices retained.

Pre-fill order: remembered mapping for this header set, else
`detect_columns(headers, load_column_mappings())`.

### Mapping memory

Stored in the existing Setup `app_settings` table, no migration:

- key: `bom_import.mapping.<sha1 of the normalised header list>` where normalisation is
  `partpilot.bom.normalize._norm` applied per header, joined by `\x1f`, order preserved;
- value: JSON `{"headers": [...original headers...], "mapping": {field: header}, "updated_at": iso}`.

Written on every successful resolve POST (so corrections overwrite). Read on upload POST. Order
preserved because a file whose columns are re-ordered is a different layout and should be
confirmed again.

### Review and result screens

Review: same five statuses and the same tick semantics as the assembly importer. `NEW` rows
pre-ticked, `UNRESOLVED` unticked, `IN_INVENTORY` shown with a link to the existing part,
`MANUAL` and `SKIP` shown greyed with the reason. Shows resolver warnings and the source
filename. Button: "Create ticked parts".

Result: created / linked / skipped counts, a table of created parts with links, and the
"needs review" list of created passives whose iLabs value notation is incomplete, each with an
edit link. A "Import another file" button returns to upload.

Setup & Tools index gets a card/row for the tool next to Part-number cleanup, following the
existing index markup and `docs/design-rules.md` (act-bar buttons, real buttons in rows, status
badges for the five statuses).

## Data flow

```
upload ─► read_table ─► headers + samples ─► pre-fill (remembered | alias guess)
      ─► user mapping ─► remember ─► resolve_bom_file(mappings=...) ─► ResolvedRun
      ─► build_import_plan ─► plan.json ─► ticks ─► apply_parts_plan ─► result
```

## Error handling

| Situation | Behaviour |
|---|---|
| No file / empty filename | Upload screen, 400, "Choose a CSV or Excel file." |
| `read_table` raises / no header row / zero columns | Upload screen, 400, the reader's message. |
| Mapping validation fails | Mapping screen, 400, error + retained choices. |
| Upload file missing at resolve (expired, restart) | Upload screen, 400, "Upload expired, please upload again." |
| Resolver raises (credentials, network) | Mapping screen, 500, "Resolve failed: …", mapping retained; upload file kept so the user can retry from the mapping screen. All job files are deleted at apply. |
| Plan missing at apply | Redirect to upload screen. |
| Same MPN twice in one file | Second occurrence links to the part created by the first (`find_part_id_by_mpn` before create, as today). |

Job ids are `uuid4().hex` and validated with `[0-9a-f]+` before touching `jobs_dir`.

## Testing

Unit (`tests/test_import_parts_mapping.py`):
- header-set key is stable across case/whitespace, differs on order and on added columns;
- pre-fill prefers remembered mapping over alias guess;
- per-run mapping dict contains only mapped fields;
- mapping validation: duplicate target rejected, missing value/device rejected.

Web (`tests/test_setup_import_parts.py`, `create_app` + temp DB + `FakeSearcher`):
- non-admin gets 403;
- upload renders every header with sample values and the alias guesses pre-selected;
- second upload with the same headers is pre-filled from the remembered mapping;
- review shows `IN_INVENTORY` for an MPN already in the catalog, `NEW` for a FakeSearcher
  hit, `UNRESOLVED` for a miss with a value, `MANUAL` for a row with neither;
- apply creates only ticked rows, links existing, reports counts;
- the ignored column's content does not reach the resolver.

Regression: the existing assembly importer tests pass unchanged after the refactor.

## Files

New: `web/features/catalog/import_parts.py`, `web/features/setup/import_parts.py` (header
reading, key, pre-fill, validation, memory), 4 templates under `setup/templates/`
(`import_parts_upload.html`, `_map.html`, `_review.html`, `_result.html`), two test files.

Changed: `assemblies/import_bom.py` (thin wrapper), `purchasing/service.py` (`mappings`
param), `setup/router.py`, `setup/templates/setup_index.html`, `docs/design-rules.md`
change log, README Setup & Tools section.

## Behaviour change callout

🟥 **SYSTEM BEHAVIOUR CHANGE:** a new admin path creates catalog parts in bulk from arbitrary
files. Parts are created with the same fields and supplier lines as the assembly importer
produces today; no existing part is modified.
