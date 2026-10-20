# Home page dashboard — design

**Status:** built 2026-10-18 … 2026-10-20 on branch `home-dashboard`, merged to main 2026-10-20.
Kept as the reference for the next iteration ("looks good for now, we may revisit later").

## Purpose

The landing page (`/`, `web/core/templates/home.html`) used to be a title and "pick a section".
Operators wanted a glance view: how big the system is, what moved recently, and which build is
running — the last one for quoting in bug reports.

## What it shows

Visible to every signed-in role (decision 2026-10-18: "Everyone"). Cards a role cannot open are
hidden, nothing else is role-specific.

### Stat cards — an even grid of 8

CSS grid `.home-grid`, `repeat(4, minmax(0, 1fr))`, equal-width cards, two rows of four. Collapses to
2 columns below 1100 px and 1 column below 560 px. Order follows nav order.

| Card | Count | Note | Rows listed | Detail link |
|---|---|---|---|---|
| Parts | active components (`kind != 'ASSY'`, `retired_at IS NULL`) | "N below minimum" | `part_no` + description (or value) | `/catalog/{id}` |
| Documents | `documents.retired = 0` | | `code title` | `/documents/{id}` |
| Assemblies | active assemblies | | `part_no` + description (or value) | `/assemblies/{id}` |
| Customer orders open | status not complete/cancelled | | `order_ref`, coloured: in progress blue, complete green, cancelled red | `/customer-orders/{id}` |
| Work orders open | status not finished/cancelled | | `wo_no · customer` (via CO line; bare for stock builds) | `/work-orders/{id}` |
| Purchase orders on order | `summary()["ordered"]` | | `po_no · supplier` | `/purchase-orders/{id}` |
| Suppliers | contacts kind=supplier | | `name` | `/contacts/{id}` |
| Customers | contacts kind=customer | | `name` | `/contacts/{id}` |

Each card: the count (a link to the list), the optional note, then **New** (4 most recently created
rows, by `created_at`) and **Edited** (4 most recently changed rows, by `updated_at`, only rows with
`updated_at > created_at`). Every entry is a link plus its ISO date. Empty list → "None yet".
Part/assembly labels take the first 80 characters of the trimmed, single-lined description (value as
fallback) — some imported descriptions are whole webshop pages.

Dropped along the way: a separate **Below minimum** card (folded into the Parts note, 2026-10-20) and
the **Article numbers** card (removed 2026-10-20 to make an even 8; the `article_numbers` table still
has the timestamps if it comes back).

### Technical panel

Under the grid, one `.panel`: **Latest changes** — the two newest git commits (date · short hash ·
subject) — and a muted line "PartPilot {version} · build {hash} ({date}) · Python · SQLite · FastAPI ·
Starlette · uvicorn". Read once at startup (`core/about.py`, cached in `app.state.about`); without
git or a repository the commit list is empty and the version line stays. The package version stays
0.1.0; the commit hash is the real build identifier.

## Mechanism

- `core/registry.py`: `HomeStat(label, value, url, note=None, newest=(), edited=())` and the optional
  `Feature.home_stats: fn(db) -> list[HomeStat]`. `FeatureRegistry.home_stats(db, role)` collects in
  nav order, skips features whose nav the role cannot see, and logs + skips a hook that raises so the
  page always renders.
- `core/home.py`: `HomeEntry(label, url, when, tone)` and `recent_entries(conn, *, table, label, url,
  where, key, tone, limit)` (`tone` = optional SQL expression → colour token per row) — the one implementation of the New/Edited rule, parameterised by SQL label
  expression, filter, key column and URL prefix.
- Each feature's `repo.py` has `home_stats(db)` next to `summary()`; `feature.py` passes it to the
  descriptor. Features never touch `home.html`.
- Home handler (`web/app.py`) passes `stats` and `about`; `home.html` renders the grid and the panel.

## Known limits / ideas for the revisit

- The bulk of the catalog was seeded 2026-06-04 (miniMRP import did not carry dates), so "New" dates
  before that are meaningless and old parts all share that day.
- A stock adjustment or version release bumps `parts.updated_at`, so those show under Edited too.
- "Below minimum" links to the plain parts list; the list has no low-stock filter yet.
- Candidates for later: a low-stock filter on `/catalog`, per-role content (open POs for purchasing),
  activity across days (a sparkline/chart), configurable list length (`RECENT_SHOWN = 4`), bringing
  back Article numbers if the 8-card constraint is relaxed.
