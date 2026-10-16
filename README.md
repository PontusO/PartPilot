# PartPilot

**PartPilot** is a small electronics manufacturer's in-house MRP: a LAN-hosted web app that holds
the parts catalog, assemblies and their versioned bills of materials, customer orders, work orders,
purchase orders, goods receipts, despatch and controlled documents — and a purchasing engine that
resolves *deliberately under-specified* BOMs (`0.1uF 0402`, a preferred-but-substitutable MPN)
into real, in-stock Digi-Key/Mouser parts with pricing and packaging.

It is a single Python package (`src/partpilot/`), a FastAPI **modular monolith** on SQLite, run as
one process on one machine that everyone on the network points a browser at. PartPilot is the
**system of record** — the previous tool, miniMRP, was retired on 2026-07-19.

> Historical note: the project started life as **DigiSearch**, a command-line BOM resolver. The
> web app grew around that engine and the package was renamed `partpilot` on 2026-09-22. The
> resolver is still available as `partpilot resolve`, and `digisearch` remains as a CLI alias.

## Quick start

```bash
uv sync                          # install (uses pyproject.toml + uv.lock)
cp .env.example .env             # fill in Digi-Key credentials (and Mouser, devmgmt if used)
uv run partpilot serve           # http://127.0.0.1:8000
uv run partpilot serve --host 0.0.0.0     # reachable from the LAN
```

On first run it prints an initial **admin** username/password (override with
`PARTPILOT_ADMIN_USER` / `PARTPILOT_ADMIN_PASSWORD`). Set `PARTPILOT_SECRET_KEY` so logins survive
restarts. Runtime data lives under `data/` (git-ignored).

For hand-testing, **never point a dev server at the live database** — use the throwaway copy:

```bash
uv run partpilot serve --scratch-db --port 8010   # copy of the DB + fresh jobs dir; discarded on exit
```

## Users and roles

Four flat roles: `admin`, `purchasing`, `warehouse`, `shipping`. Any signed-in user can *read*
every screen; writes are gated per feature — broadly `admin`+`purchasing` edit master data and
orders, `warehouse` joins them for stock movements, receiving, work orders and despatch,
`shipping` can despatch, and only `admin` sees **Setup & Tools**. Users are managed under Setup.

## What's inside

Screens in navigation order. Each is a self-contained feature module under
`src/partpilot/web/features/<name>/`.

### Parts (`catalog`)
Searchable, paged parts list (category filter, stocked-only) and a per-part page with supplier
offers, stock, movement history and locations. Add/edit components with multiple supplier lines
(one default), flags (`unlimited_stock`, `normally_stocked`, `exclude_from_bom_cost`) and a
per-part overhead factor. **Costing is two-layer:** captured supplier *cost* tiers → *loaded* cost
tiers (cost × overhead), either generated from the default supplier's breaks or refreshed live from
Digi-Key/Mouser. Stock movements (receive / issue / adjust) are posted from here, per shelf.
A part can be **retired** (hidden from lists and pickers, never suggested for purchase or matched
by the resolver, existing stock runs out) and, once retired and unreferenced by any order, work
order, movement or BOM, **deleted** by an admin — the lifecycle applies to every version of the part.

**Stock shelves (customer-owned stock).** A *shelf* is a stock location with an owner: **ours**
("Main", plus any others you add) or a **customer's**. Customer shelves hold parts the customer paid
for up front — only their work orders draw from them, and they never count in our totals,
valuation or the webshop (`parts.total_qty` is *our* stock; `parts.customer_qty` is theirs). Create
a per-customer shelf from the customer's contact page, or a per-job shelf from a customer order;
browse all shelves under Setup & Tools → **Stock shelves**. The part page shows stock by shelf.
Leftovers stay on the customer's shelf; there is no automatic transfer.

### Article Register
The internal part-number allocator, replacing the old Excel *Artikelregister*. Numbers are
`PREFIX-NNNNN-S`: a 2-digit class prefix, a 5-digit running number that identifies the *product
family*, and a suffix. Allocate a single number, a whole family from ticked prefixes, or from a
saved template; browse by family. Document-class prefixes (50–59, 95 software) materialise as
Documents, other classes as catalog part stubs. Seed from the legacy workbook with
`partpilot import-article-register FILE.xlsx`.

### Documents
Controlled documents stored against article numbers, with a **revision history** (upload a new
file revision or a new link revision, choose which is current, retire). Prefix 95 (software/source)
is always a link — typically to GitHub — never a stored file. Files live on disk under
`data/documents/`, **not** in the database (see Backups).

### Assemblies
Assemblies are parts with `kind = ASSY`; their structure is a `bom_lines` tree (parent → child), so
sub-assemblies and *where-used* come for free. The assembly page shows the BOM, where-used, the
family's documents, and material / loaded price cards at a chosen build quantity; **Refresh prices
& estimate** re-queries distributor tiers for the components you would have to buy. BOM lines are
edited inline, the whole BOM downloads as `.xlsx`, and **Import BOM** runs a CSV through the same
resolver as Purchasing (resolve → review → apply: existing parts linked, resolved parts offered as
new, under-specified lines tagged *manual*). A devmgmt panel publishes the product's
model/variant (see Integrations). **Retire / Unretire / Delete** follow the part lifecycle above: a
retired assembly takes no new work orders but its finished boards can still be despatched.

### Versioning (parts and assemblies)
Several versions of the same part number can coexist. **New version** clones a part or assembly
(BOM included) as a **draft**; **Release** promotes it and marks the previous release
**superseded** — exactly one released version per group. Lists collapse to the current version
(with a *+N ver* badge), pickers and the resolver's own-stock check see only released versions,
and resolving by number prefers the released one.

### Customer Orders
`CO-NNNNN` orders for a customer with lines, addresses, discount, delivery charge and tax.
Issue an **acknowledgement PDF** (archived immutably, advances *draft → confirmed*), **allocate**
stock against lines, see downstream work orders / despatches and a shortage / build proposal.
Statuses beyond `confirmed` are set by the system: `shipped` by despatch, `complete` by invoicing,
`cancelled` by Cancel (refused once goods have shipped).

### Work Orders
Build an assembly: creating a WO explodes its BOM and snapshots required quantities with the
configured spillage % / minimum margin applied. Every WO builds from **one shelf** (pre-set to the
customer's job/customer shelf when raised from their order): availability, issue and the finished
boards all stay on that shelf — a customer build is short on their shelf even when Main has the
part, and never consumes our stock. On a customer shelf the material is customer-supplied: the WO
shows zero charged with the reference value alongside. Lifecycle: **allocated → issued → finished**.
*Issue* posts ISSUE stock movements for every component (except `unlimited_stock` cost-only lines
such as SMT assembly); *Finish* posts the built assembly into stock and queues a devmgmt push;
*Flush* does both. *Regenerate BOM* re-explodes an allocated WO whose assembly BOM has drifted (the
list flags these). WOs can also be generated in bulk from a customer order's fulfilment proposals.

### Planning
A calendar of work orders. Drag to move the build window, resize to change the due date, or drag
the *purchase-by* marker on its own.

### Purchase Orders
**Suggestions** computes what allocated work orders need beyond free stock and open POs, **per
shelf**, grouped by shelf then supplier — tick and confirm to create one draft PO per supplier per
shelf, tier-priced by a live distributor lookup. Every PO has a *receive-into shelf* (shown on the
PDF); receiving posts the goods onto that shelf. Manual POs too. **Placing** a PO freezes its supplier CSV + PDF as immutable records; **receiving** posts
RECEIVE movements and creates a goods receipt. Delete is refused once anything has been received.

### Goods Receipts
Read-only register of GRNs created by the PO receive flow, with PDF/CSV downloads.

### Purchasing (the BOM resolver)
Upload a BOM (`.csv/.tsv/.txt/.xlsx/.xlsm/.xls`), give a build quantity, pick the **shelf to check
stock on** (Main, a customer's shelf, or none — the same selector appears on the assembly BOM
importer and the Setup parts importer), and get every line
resolved to an orderable part with status colouring, an Excel report and one cart CSV per
distributor. How it works is described in [The BOM resolver](#the-bom-resolver) below.

### Despatch
Ship from a *confirmed* customer order: create a **packing list**, tick off lines as packed,
confirm ready, then **dispatch** — the only step that moves stock (ISSUE movements, allocation
consumed, order flipped to `shipped` when complete) and produces the despatch note. Fortnox
invoicing hooks in here when connected (see Integrations).

### Contacts
Address book of suppliers, customers and others, each with structured addresses (default delivery /
invoice). Contacts is the **only supplier record**: the part-edit and PO supplier dropdowns list
supplier-kind contacts, and the PO PDF prints the supplier's address from here. (The legacy catalog
`suppliers` table was merged away on 2026-10-16.)

### Reports
Stock-movement ledger, filterable by date range, movement type and shelf.

### Manual
The end-user manual, [`docs/user-manual.md`](docs/user-manual.md), rendered in-app at `/help` for
every role (nav 📖 Manual). Organised by workflow and quoting the real button labels — keep it in
step when a screen or rule changes.

### Setup & Tools (admin)
**Import parts from BOM** (upload any CSV/Excel part list, map its columns to PartPilot fields —
the mapping is remembered per header layout — resolve rows against stock and Digi-Key/Mouser,
choosing which **shelf** to check stock on — a customer's shelf sees only their parts, so rows we hold on Main are still priced —
tick the parts to create; the summary lists every row's outcome and the parts it could not find, and can wrap all rows with a part — new and pre-existing — into a new assembly);
Users (roles, activation, password reset — with guardrails against locking yourself out);
Company details (used on PO PDFs); Production settings (spillage %, minimum margin);
**Stock shelves** (our and customer-owned shelves: owner, contents, value; rename / retire);
Order and Pricing settings (default overhead factor); **Part-number cleanup** (find parts whose
part number is really a distributor order code and fix the MPN); Webshop (WooCommerce) settings
and sync; Fortnox connection.

## The BOM resolver

The engine behind Purchasing, the assemblies BOM importer, Setup → Import parts and `partpilot resolve`. Per line:

| Line kind | Detected by | Action |
|-----------|-------------|--------|
| Generic passive | `Device` like `C_CHIP-0402…`, `R_CHIP-…`, `L_CHIP-…` + a value | Parametric search from value + package; assumes a default tolerance / dielectric / voltage and **flags** for review |
| Crystal | crystal-like device + frequency | Parametric search |
| Real MPN | `Value`/`Device` looks like a manufacturer part number | Keyword search; best in-stock active match |
| Lookup | matches a rule in `config/device_lookup.yaml` | Uses the rule's MPN / query |
| Do-not-mount | value `DNM`/`DNP`/… | Kept in the report, marked DNP, not priced |
| Non-orderable | testpoints, mounting holes, pads | Skipped from purchasing, listed for completeness |
| Manual | no parseable value or MPN (`C38`, `SV1`) | Flagged for a human — never resolved to a guess |

1. **Own stock first.** In the web flows the resolver checks PartPilot's catalog (by MPN for
   ICs/connectors, by value + package for passives) and, if free stock (`on hand − allocated`)
   covers the build, marks the line **in stock** and skips the distributor call; partial stock
   bills only the shortfall.
2. **Digi-Key**, then **Mouser only when Digi-Key is weak** (not found, below the confidence
   threshold, or out of stock) and only if Mouser out-scores it. Leave `MOUSER_API_KEY` blank to
   disable Mouser.
3. Candidates are **scored** on value / package / stock / lifecycle / type (weights in
   `config/settings.yaml`); the best in-stock candidate is chosen, low-confidence rows are flagged
   **review**.
4. **Full reel vs cut tape:** if a full reel costs less than `reel_threshold` (default 10 000 in the
   locale currency) the whole reel is ordered — cheaper per part, excess to stock — otherwise cut
   tape for the exact shortfall. Multi-reel lines round up to whole reels.

**Outputs:** an Excel workbook (resolved lines, status, packaging, order qty, unit and line cost,
alternates) and upload-ready cart CSVs — `…-digikey-cart.csv` for Digi-Key's *Upload a List* and
`…-mouser-cart.csv` for Mouser's *BOM Import* — containing only confidently-resolved lines, using
the Tape & Reel or Cut Tape part number to match the packaging decision. Flagged lines go to
`…-needs-review.csv`. PartPilot never places an order; you review the cart and check out.

### Digi-Key and Mouser credentials

Digi-Key: the **Product Information API v4** with the 2-legged OAuth2 client-credentials grant.

1. Create an account and an Organization at <https://developer.digikey.com>.
2. Create a **Production App** subscribed to **Product Information V4** (the OAuth callback URL is
   unused by the 2-legged flow — any HTTPS URL works).
3. Put the Client ID and Secret in `.env`; run `uv run partpilot auth-test`.

Production is the default; the sandbox (`DIGIKEY_SANDBOX=true`, `--sandbox`) returns mock data and
needs separate credentials. The free tier is rate-limited (~1000 calls/day), so every response is
cached on disk under `.partpilot_cache/`.

Mouser: request a Search API key at <https://www.mouser.com/api-hub/> and set `MOUSER_API_KEY`.

## CLI

```bash
uv run partpilot serve [--host H] [--port P] [--reload] [--scratch-db]
uv run partpilot resolve BOM.csv [--build-qty N] [-o OUT.xlsx] [--currency SEK] \
                                 [--reel-threshold N] [--purchase] [--dry-run] \
                                 [--map MAP.yaml] [--lookup LOOKUP.yaml] [--sandbox]
uv run partpilot auth-test                          # verify Digi-Key credentials
uv run partpilot import-article-register FILE.xlsx  # seed the Article Register (one-off)
uv run partpilot devmgmt-push [--serial S] [--dry-run] [--seed-demo]
```

`resolve --dry-run` classifies lines and builds queries without any API call. `--purchase` also
writes the cart CSVs. **CLI flags override `config/settings.yaml`, which overrides built-in
defaults.**

## Configuration and data

| Where | What |
|-------|------|
| `.env` (git-ignored, from `.env.example`) | `DIGIKEY_CLIENT_ID/SECRET`, `DIGIKEY_SANDBOX`, `DIGIKEY_LOCALE_*`, `MOUSER_API_KEY`, `DEVMGMT_*`, `PARTPILOT_SECRET_KEY`, `PARTPILOT_ADMIN_USER/PASSWORD`, `PARTPILOT_DATA_DIR`, `PARTPILOT_DB` |
| `config/settings.yaml` (git-ignored, from `settings.example.yaml`) | resolver defaults: `build_qty`, `currency`, `reel_threshold`, `confidence_threshold`, match `weights`, default passive tolerance/dielectric/voltage, `dnp_values` |
| `config/column_mappings.yaml` | BOM column aliases (which header means *Value*, *Device*, *Refdes*, …) |
| `config/device_lookup.yaml` | device → MPN / query rules |
| `data/partpilot.db` | the SQLite database (WAL mode) |
| `data/documents/` | uploaded document revisions |
| `data/jobs/` | uploaded BOMs and generated reports/carts |
| `.partpilot_cache/` | cached Digi-Key responses + token |

**Backups:** `data/partpilot.db` **and** `data/documents/` — the documents are not in the DB.

## Deployment

Production runs as the systemd unit `partpilot.service` (see `deploy/partpilot.service`):
`WorkingDirectory=/home/service1/partpilot`, `EnvironmentFile=…/.env`,
`ExecStart=…/.venv/bin/partpilot serve --host 0.0.0.0 --port 8000`.

```bash
sudo systemctl restart partpilot.service
```

Python code is imported once at startup, so **restart after changing server-side code or `.env`**.
Jinja templates hot-reload from disk, which is why a half-updated UI usually means "the handler
hasn't been restarted yet". Migrations are additive and run on startup.

## Integrations

- **devmgmt** (live, mTLS) — PartPilot publishes product models/variants from the assembly page and
  per-device build records when a work order finishes, via a persistent outbox with retry/backoff.
  Contract in [`docs/partpilot-integration.md`](docs/partpilot-integration.md).
- **WooCommerce** — read-only pull of webshop products matched by SKU (Setup → Webshop), on demand
  or as a daily background sync.
- **Fortnox** (parked) — OAuth connection and automatic invoice creation on dispatch; dormant
  without credentials.

## Architecture in brief

A thin **core** (`web/core/`) owns auth, navigation, the SQLite database + migration runner and the
shared template environment. Everything else is a **feature module** — `feature.py` exporting a
`Feature` descriptor (router, nav entry with role gating, ordered migrations, template dir) plus
`router.py`, `repo.py` (raw `sqlite3`, no ORM), `migrations.py` and `templates/`. Adding a feature
is appending it to `FEATURES` in `web/app.py`; the core never edits features and features never
edit the core. Migrations are tracked per `(feature, version)` and never edited once shipped.

For contributors: [`CLAUDE.md`](CLAUDE.md) covers the development process and architecture
detail; [`docs/design-rules.md`](docs/design-rules.md) is the source of truth for the UI (tokens,
button system, action bars, tables and forms).

Migrations are SQL scripts or Python callables (for steps that must inspect the live schema).
Before applying any pending migration to an existing database the runner writes a copy to
`data/backups/partpilot-<timestamp>.db`; a failed backup aborts startup.

## Tests

```bash
uv run pytest                     # everything (~600 tests, no live API calls)
uv run pytest -k mouser           # by keyword
```

HTTP is mocked with `respx`; `tests/conftest.py` provides Digi-Key / Mouser response builders and a
`FakeSearcher` for resolution logic. Web tests build an app against a temp database.

## License

PartPilot is released under the [MIT License](LICENSE). Digi-Key, Mouser, devmgmt, WooCommerce
and Fortnox are third-party services with their own terms; you need your own credentials for each.
