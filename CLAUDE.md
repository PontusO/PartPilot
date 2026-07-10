# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Two products in one package (`src/digisearch/`):

1. **DigiSearch CLI** — resolves *deliberately under-specified* BOMs (generic passives like
   `0.1uF 0402`, preferred-but-substitutable MPNs) into orderable parts by matching each line to a
   real Digi-Key part (Mouser as fallback), with stock, lifecycle, pricing and packaging. Outputs
   an Excel report + distributor cart CSVs.
2. **PartPilot web app** — an internal, LAN-hosted FastAPI tool (modular monolith) that wraps the
   purchasing engine in a browser UI and is growing into a full MRP (catalog, assemblies, orders,
   work orders, despatch, planning). The package name is still `digisearch`; the product is
   PartPilot.

The README is unusually complete — read it for domain detail (how lines are classified, reel-vs-cut-tape
rules, Digi-Key/Mouser credential setup, miniMRP stock checking). This file covers what the README doesn't.

## Commands

This project uses `uv`. Always prefix Python commands with `uv run`.

```bash
uv sync                                    # install deps (incl. dev group)
uv run pytest                              # run all tests
uv run pytest tests/test_pipeline_report.py            # one test file
uv run pytest tests/test_pipeline_report.py::test_name # one test
uv run pytest -k "mouser"                  # tests matching a keyword

uv run digisearch auth-test                # verify Digi-Key credentials
uv run digisearch resolve slice-vb.csv --build-qty 100 -o out.xlsx   # CLI resolve
uv run digisearch resolve slice-vb.csv --dry-run         # classify + build queries, no API calls
uv run digisearch serve --reload           # web app at http://127.0.0.1:8000 (dev auto-reload)
uv run digisearch import-catalog           # seed PartPilot DB from miniMRP (uses settings minimrp_path)
```

There is no linter/formatter configured. `requirements.txt` exists but `pyproject.toml` + `uv.lock`
are the source of truth for dependencies.

## Deployment (production web app)

Production runs the web app as a **systemd service** `partpilot.service` (unit at
`/etc/systemd/system/partpilot.service`): `WorkingDirectory=/home/service1/partpilot`,
`EnvironmentFile=…/.env`, `ExecStart=…/.venv/bin/digisearch serve --host 0.0.0.0 --port 8000`.

**After changing server-side Python (routers, repos, `app.py`, etc.) or `.env`, restart the
service** — the process imports modules once at startup, so code and env changes are NOT picked up
until a restart (needs sudo):

```bash
sudo systemctl restart partpilot.service
```

Gotcha that looks like a bug: Jinja templates **hot-reload from disk** on every request, but Python
code does not. Editing a template *and* its route handler, then not restarting, renders the new
template against the old handler's context — variables come through undefined (e.g. a panel showing
"not configured" because the handler that passes `devmgmt_configured` hasn't loaded yet). If the UI
looks half-updated, restart before debugging. Migrations are additive and run on startup, so a
restart also applies any new ones.

## Tests

- `pytest` config lives in `pyproject.toml` (`testpaths = ["tests"]`).
- Tests never hit live APIs. `respx` mocks HTTP; `tests/conftest.py` provides `make_product`
  (Digi-Key v4 response shape), `make_mouser_part`, and `FakeSearcher` (a stand-in implementing the
  `Searcher` protocol). When testing resolution logic, inject a `FakeSearcher`, not a real client.
- Web feature tests construct an app via `create_app(...)` with a temp DB; migrations run on startup.

## Architecture

### The resolution engine (CLI core — `pipeline.py`, `spec/`, `match/`)

This is the heart of both products. `resolve_bom()` → `resolve_line()` per line. Flow per line:

1. **`plan_line()`** classifies the line (`spec/classify.py`) into a `LineKind` (GENERIC_PASSIVE,
   CRYSTAL, MPN, LOOKUP, DNP, NON_ORDERABLE), applies the device-lookup table
   (`config/device_lookup.yaml`), and builds a search query + `PartSpec`. This is the no-API,
   `--dry-run` path.
2. Lines too under-specified to resolve (no parseable value/MPN) short-circuit to `Status.MANUAL`.
3. **miniMRP stock pre-check** (if a stock index is loaded): if free stock covers the build, mark
   `IN_STOCK` and skip the API entirely (saves rate-limited quota).
4. Search Digi-Key (`Searcher` protocol). **Mouser is consulted only when Digi-Key is weak**
   (no/low-confidence match, or DK best is out of stock) and wins only if it out-scores DK.
5. **`match/score.py` `rank()`** scores candidates on value/package/stock/lifecycle/type
   (weights in `Settings.weights`). Best in-stock candidate auto-selected;
   `< confidence_threshold` → `Status.REVIEW`.
6. **`purchasing.py` `decide_packaging()`** chooses full-reel vs cut-tape by `reel_threshold`.

`models.py` holds the dataclasses threaded through everything: `BomLine` (input) → `ResolvedLine`
(output, carries `Status`, chosen `Candidate`, packaging, costs). `Status` drives the report and
cart filtering. Supplier clients (`digikey/`, `mouser/`) convert API JSON to `Candidate` and satisfy
the `Searcher` protocol — keep that protocol boundary clean so the engine stays supplier-agnostic.

The web app reuses this engine via `resolve_bom_file()` — resolution improvements flow to both the
purchasing screen and the assemblies BOM importer. Don't fork resolution logic into the web layer.

### The web app (PartPilot — `src/digisearch/web/`)

**Modular monolith.** A thin **core** (`web/core/`) owns auth, nav, the SQLite DB + migration
runner, and the shared Jinja template environment. Everything else is a **feature module** under
`web/features/<name>/`.

- **Adding a feature = appending to `FEATURES` in `web/app.py`.** The core never edits features and
  features never edit the core. Order in that list matters: it controls migration order, so a
  feature whose tables have FKs into another's must be registered *after* it (see the comments in
  `app.py` — e.g. `customer_orders` before `work_orders`).
- Each feature ships a `feature.py` exporting a `Feature` descriptor (`web/core/registry.py`):
  `router`, `nav` (a `NavItem` with role gating + sort `order`), `migrations`, `template_dir`.
  Conventional file layout per feature: `feature.py`, `router.py`, `repo.py` (all SQL — raw
  sqlite3, no ORM), `migrations.py`, `templates/`.
- **Migrations** are ordered `Migration(version, name, sql)` per feature, tracked in
  `schema_migrations` keyed by `(feature, version)`. They run on every startup and on
  `import-catalog`; only unseen ones apply. **Never edit a shipped migration — add a new one.**
- **Routers depend only on `web/core/deps.py`**, not on `create_app`. Get platform services from
  `request.app.state` (`store`, `registry`, `database`, `templates`, `jobs_dir`). Gate actions with
  `require_role(request, roles)` / `require_user(request)`.
- **Roles** (`web/auth.py`): `admin`, `purchasing`, `warehouse`, `shipping`. `PURCHASE_ROLES =
  {admin, purchasing}`. Nav visibility and route gating both use these. Not-yet-built sections are
  greyed placeholder nav entries via `make_placeholder()`.
- DB uses WAL mode (concurrent readers + single writer) for multi-user LAN access.

### miniMRP as the system of record (for now)

PartPilot's SQLite is seeded from [miniMRP](https://minimrp.com/) (a Microsoft Access DB read via
`mdbtools`). `import-catalog` upserts on a stored `minimrp_id` so it's **safe to re-run** during the
dual-run period — miniMRP stays authoritative while features migrate the data in. Importers live in
each owning feature (`catalog/importer.py` parts/suppliers/stock, `assemblies/importer.py` BOM tree
from `tblusedin`, `contacts/importer.py` address books). Pricing gotcha: miniMRP's `PriceEach` is
per-reel (per `QtyPerUOM`); per-piece price is `PriceEach / QtyPerUOM`.

### External integrations

`fortnox/` (accounting — invoices), `woocommerce/` (webshop sync). These are optional, configured
via the **Setup & Tools** feature (admin only) and `.env`. Tests mock them.

## Configuration & data

- **Credentials** live in `.env` (git-ignored): `DIGIKEY_CLIENT_ID/SECRET`, `MOUSER_API_KEY`
  (optional — blank disables Mouser), `DIGIKEY_SANDBOX`, locale vars. Loaded via
  `config.py` (`DigiKeyCredentials.from_env`, `MouserCredentials.from_env`).
- **Settings** (`config/settings.yaml`, git-ignored; copy from `settings.example.yaml`) hold
  operational defaults (`minimrp_path`, `build_qty`, `currency`, `reel_threshold`, match `weights`).
  **CLI flags always override the settings file**, which overrides built-in defaults — see how
  `cli.py resolve()` resolves each value.
- `config/column_mappings.yaml` (BOM column aliases) and `config/device_lookup.yaml` (device→MPN/query
  rules) tune resolution; overridable per-run with `--map` / `--lookup`.
- **Runtime data** lives under `data/` (git-ignored): `partpilot.db` (back this up),
  `jobs/` (uploaded BOMs + generated reports). Paths resolve via `web/core/paths.py` and honor
  `PARTPILOT_DATA_DIR` / `PARTPILOT_DB` env overrides.
- Web env: `PARTPILOT_SECRET_KEY` (session signing — set it so logins survive restarts),
  `PARTPILOT_ADMIN_USER/PASSWORD` (initial admin, generated + printed on first run if unset).
- Digi-Key responses are cached on disk in `.digisearch_cache/` (free tier ~1000 calls/day).
