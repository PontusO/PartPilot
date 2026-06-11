# DigiSearch

Resolve **simplified BOM documents** into real, orderable BOMs by matching each line to a
concrete Digi-Key part — carrying reference designators, manufacturer part numbers, Digi-Key
part numbers, live stock, lifecycle, datasheet links, and both unit and price-break pricing.

Your BOMs are deliberately under-specified (generic passives like `0.1uF 0402`, or a preferred
MPN that may have a better in-stock alternate) to keep design and production flexible.
DigiSearch turns those into purchasable parts and flags anything that needs a human decision.

## Quick start

```bash
uv sync                              # install dependencies
cp .env.example .env                 # then fill in your Digi-Key credentials
uv run digisearch auth-test          # verify API credentials (production by default)
uv run digisearch resolve slice-vb.csv --build-qty 100 -o slice-vb-resolved.xlsx
```

## Web app (PartPilot)

DigiSearch also ships a small **web front-end** so colleagues can run the **purchasing** flow
from a browser instead of the CLI — the first step toward an in-house tool the whole company uses.

```bash
uv run digisearch serve                 # local only: http://127.0.0.1:8000
uv run digisearch serve --host 0.0.0.0  # allow other machines on the LAN to connect
```

It's an *internal* app: one machine on your network runs a single process, everyone points a
browser at it. On first run it prints an initial **admin** username/password (override with
`PARTPILOT_ADMIN_USER` / `PARTPILOT_ADMIN_PASSWORD`). Log in, upload a BOM, pick a build
quantity, and you get the resolved purchasing table plus downloadable Excel report and
distributor cart CSVs — the same engine as the CLI.

Access is **role-based**: only the `admin`/`purchasing` roles can run purchasing (warehouse/
shipping roles are reserved for screens still to come). Users, uploaded BOMs and generated
files live under `data/` (git-ignored); back up `data/partpilot.db`. Set
`PARTPILOT_SECRET_KEY` to keep logins valid across restarts. For production, run it behind a
`systemd` unit on one LAN machine.

> This is a **purchasing** aid — it resolves a BOM to orderable parts, checks stock and builds
> distributor carts. (A dedicated customer-quotation tool may reuse parts of this later, but
> that's a separate tool.)

### Architecture: feature modules

PartPilot is a **modular monolith**. A small **core** (`src/digisearch/web/core/`) owns the
cross-cutting concerns — auth/login, navigation, the SQLite database + a migration runner, and
the shared template environment. Functionality is added as bounded **feature modules** under
`src/digisearch/web/features/<name>/`, each declaring what it contributes via a `Feature`
descriptor (`feature.py`):

```python
feature = Feature(
    name="catalog",
    router=router,                                  # its FastAPI routes
    nav=NavItem("Parts", "/catalog", roles=frozenset({"admin", "purchasing"})),
    migrations=[Migration(1, "create parts", "CREATE TABLE parts (...);")],
    template_dir=Path(__file__).parent / "templates",
)
```

The core includes the router, runs the migrations on startup, and shows the nav entry only to
the roles allowed — the feature never edits the core. To add functionality you create a feature
package and append it to `FEATURES` in `web/app.py`; nothing else changes. Purchasing
(`features/purchasing/`) is the reference example. (This is internal extensibility — there is no
third-party plugin API.)

### Parts catalog

The **catalog** feature (`features/catalog/`) is the first feature to *own* data: a normalized
`parts` / `suppliers` / `part_suppliers` / `stock_locations` / `part_stock` schema in PartPilot's
SQLite, seeded from miniMRP. Import (or re-sync) it with:

```bash
uv run digisearch import-catalog            # uses settings 'minimrp_path'
uv run digisearch import-catalog --from /path/to/miniMRP/Data/mrp5data
```

The import upserts on a stored `minimrp_id`, so it's safe to re-run while miniMRP stays the
system of record (dual-run); it imports parts, suppliers, stock, the assembly BOM structure
**and the contacts address book** (suppliers/customers/misc). The same import is also available in the web GUI under **Setup & Tools → Import from
miniMRP** (admin only), where you can **browse/upload any `mrp5data` file** or leave it empty to
use the configured default path. The **Parts** screen then offers a searchable, category-filterable list with stock,
default supplier and per-piece price, and a per-part detail view (all supplier offers + stock by
bin). It shows **components only** (assemblies live under their own tab). The **+ Add component**
button (admin/purchasing roles) opens a form to create a new part with all its fields, multiple
supplier lines (pick an existing supplier from the dropdown or add a new one inline; unit price +
reel qty), and opening stock. Each part's detail page has an **Edit** button that reuses the same
form to maintain every field, its supplier lines and its stock. Dedicated stock movements
(receive/issue/adjust) come next.

> Note on pricing: miniMRP's `PriceEach` is the price per `QtyPerUOM` (per reel), so the
> per-piece price shown is `PriceEach / QtyPerUOM` (verified against miniMRP's own `xCost`).

### Assemblies

The **assemblies** feature (`features/assemblies/`) models the multi-level BOM tree. Assemblies
are parts with `kind = ASSY`; their structure lives in a `bom_lines` table (parent → child, both
referencing `parts`, so subassemblies and where-used come for free). It's seeded from miniMRP's
`tblusedin` by the same `import-catalog` command. The **Assemblies** tab lists every assembly with
its BOM-line and used-in counts; each assembly's page shows its bill of materials (qty · component
· reference designators), with subassembly children as links you drill into, plus a **Used in**
list of the assemblies that consume it. The BOM is **editable** (admin/purchasing): the **Add
Component to Assy** button opens a dialog where you filter the full part list and click a part to
add it (with qty + reference designators), and each line has a ✕ to remove it — granular edits
that preserve the imported lines' `minimrp_id`. **New Assy** creates an assembly from scratch.

The **Import BOM (.csv)** button resolves an uploaded BOM with the **same engine as the
purchasing tool** (shared `resolve_bom_file()`, so resolution improvements flow to both), then
shows a **review screen**: lines already in inventory are linked automatically; lines resolved on
Digi-Key/Mouser are offered as **new parts** to create (tick to accept, with a link to the
distributor page — opens in a new tab — to verify), and unresolved lines can be created bare from
their CSV text. Lines the BOM under-specifies so badly the tool can't pick a part (no value/MPN —
valueless passives like `C38`/`L1`, or generic headers like `SV1`) are tagged **manual**: they
need a human and can't be auto-imported. Applying creates the accepted parts (with a supplier line
from the resolved candidate) and adds a BOM line per row. *Building* an assembly (consuming stock,
via Work Orders) comes next.

### Contacts

The **contacts** feature (`features/contacts/`) is a unified address book of business contacts —
**suppliers, customers and other** companies — in one `contacts` table with a `kind`
discriminator (miniMRP keeps these in three separate tables, `tblsup/cus/misaddresses`, which we
fold into one). It holds company name, short name, contact person, email/phone/fax, address,
postcode, website, currency, discount and notes. The **Contacts** tab lists them with a kind
filter + search, and **New Contact** / **Edit** forms (admin/purchasing) let you add and maintain
them — the foundation for Customer Orders (customers) and Work Orders / purchasing (suppliers).
It's seeded from miniMRP by the same `import-catalog` command. (The catalog's lean `suppliers`
table — used for per-part supplier links — stays separate for now; the two will be unified when
purchase orders are built.)

> The same **manual** status applies in the purchasing tool — under-specified lines are flagged
> rather than resolved to a wrong part.

## Checking what's already in stock (miniMRP)

If you keep inventory in [miniMRP](https://minimrp.com/), point DigiSearch at its database to
skip re-purchasing parts you already have — and to recover parts that aren't even on Digi-Key:

```bash
uv run digisearch resolve slice-vb.csv --build-qty 100 \
    --check-stock "/path/to/miniMRP/Data/mrp5data" -o out.xlsx
```

For each line it matches your stock (by MPN for ICs/connectors, by value+package for passives),
compares free stock (`on-hand − allocated`) against the quantity the build needs, and:

- marks a line **`in_stock`** (no purchase) when free stock covers the build,
- bills only the **shortfall** on Digi-Key when stock is partial,
- adds **In stock / Need to buy / miniMRP match** columns to the report.

Fully-stocked lines skip the Digi-Key call entirely, saving API quota. Requires **mdbtools**
(`sudo apt install mdbtools`) to read the Access database.

## Getting Digi-Key API credentials

DigiSearch uses the **Product Information API v4** with the 2-legged OAuth2
*client-credentials* grant — no per-user browser login is required for product search/pricing.

1. Create an account at <https://developer.digikey.com> and an Organization.
2. Under the organization, create a **Production App** subscribed to **Product Information V4**.
   (The OAuth callback URL it asks for is unused by the 2-legged flow — any HTTPS URL works.)
3. Copy the **Client ID** and **Client Secret** into `.env`.
4. Run `uv run digisearch auth-test` to confirm the credentials work.

Production returns real pricing/stock and is the default. The sandbox (`--sandbox`,
`DIGIKEY_SANDBOX=true`) only returns mock data and needs separate sandbox-app credentials.

The free tier is rate-limited (~1000 calls/day), so DigiSearch caches every API response on
disk under `.digisearch_cache/`.

### Mouser as a second-choice supplier (optional)

Digi-Key is always preferred. If you also set a **Mouser Search API key**
(`MOUSER_API_KEY`, from <https://www.mouser.com/api-hub/>), DigiSearch consults Mouser **only
when Digi-Key's best match is weak** (not found or below the confidence threshold) and picks
it only if it out-scores Digi-Key — so it rescues parts like Mouser-exclusive ICs without
changing anything Digi-Key already resolves well. The chosen distributor is shown in the
report's **Supplier** column. Leave `MOUSER_API_KEY` blank to disable Mouser entirely.

## How lines are resolved

| Line kind | Detected by | Action |
|-----------|-------------|--------|
| Generic passive | `Device` like `C_CHIP-0402…`, `R_CHIP-…`, `L_CHIP-…` + a value | Parametric keyword search built from value + package; assumes a default tolerance/dielectric/voltage and **flags** for review |
| Real MPN | `Value`/`Device` looks like a manufacturer part number | Keyword-searches the MPN; picks best in-stock active match |
| Do-not-mount | value `DNM`/`DNP` | Kept in the report, marked DNP, not priced |
| Non-orderable | testpoints, mounting holes, test pads | Skipped from purchasing, listed for completeness |

Matches are scored on parametric agreement, stock, lifecycle and price; the best in-stock
candidate is auto-selected, and low-confidence rows are **flagged** in the output workbook.

## Full reel vs cut tape

For parts that come on a reel, DigiSearch decides whether to buy the **whole reel** or just
**cut tape** for the quantity the build needs. The rule: if a full reel costs **less than
`--reel-threshold`** (default 10000, in the locale currency) it orders the whole reel — cheaper
per part, and the excess goes to stock; otherwise it orders cut tape for the exact shortfall.
Lines needing more than one reel round up to whole reels. Pass `--reel-threshold 0` to always
use cut tape. The report shows **Packaging**, **Order qty**, **Order unit price** and **Line
cost**, and flags full-reel buys that overshoot the build ("extra to stock").

## Purchasing (`--purchase`)

Adding `--purchase` writes upload-ready **cart CSVs** for the parts that still need buying
(chosen part, `purchase_qty > 0`, not in stock) — one per distributor:

- `…-digikey-cart.csv` — upload to Digi-Key's *Upload a List / BOM Manager*
- `…-mouser-cart.csv` — upload to Mouser's *BOM Import*

Each row is `quantity, distributor part number, manufacturer part number, customer reference
(refdes)`. The part number matches the packaging decision — the **Tape & Reel** P/N for full
reels, the **Cut Tape** P/N otherwise. Both importers turn the list into a cart you review and
check out manually (DigiSearch never places an order).

Only **confidently-resolved** lines go into the carts. Lines flagged for review are written
separately to `…-needs-review.csv` (with supplier, quantity, packaging, part numbers,
confidence and the flag reason) so you can verify them and add them to a cart by hand.

> Digi-Key has no direct cart API; a fully automated path would use their MyLists API, which
> needs 3-legged OAuth (a one-time browser login). The CSV upload avoids that entirely.

## CLI

- `digisearch auth-test` — fetch a token and confirm credentials/headers work.
- `digisearch resolve INPUT [--build-qty N] [-o OUT.xlsx] [--sandbox] [--currency SEK] [--check-stock mrp5data] [--reel-threshold 10000] [--map MAP.yaml] [--lookup LOOKUP.yaml]`

## Configuration (settings file)

`config/settings.yaml` (copy from `config/settings.example.yaml`) holds your **default input
parameters** so you don't retype them — `minimrp_path`, `build_qty`, `currency`, `output_dir`,
plus tuning like `reel_threshold` and matching weights. It's git-ignored, so machine-specific
paths are fine to keep there. **CLI flags always override the file.** With it set up, a full run
is just:

```bash
uv run digisearch resolve slice-vb.csv
```

which auto-loads stock from `minimrp_path`, builds for `build_qty`, prices in `currency`, and
applies the reel threshold — all from the file. See `config/settings.example.yaml` for every key.
