# PartPilot GUI design rules

The single source of truth for how the PartPilot web UI looks and behaves. Read this **before doing
any design/UI work** (new templates, buttons, tables, forms, colours, layout). When we make a new
design decision, record it here so the next change stays in sync.

The **reference implementation** is the assembly BOM page
(`web/features/assemblies/templates/assembly_detail.html`) together with the shared stylesheet in
`web/core/templates/base.html`. When in doubt, copy those, don't invent.

> Rule of thumb: if a page needs a widget, first check whether `base.html` (or the assembly BOM page)
> already defines it. Reuse the existing class. Only add new CSS when nothing fits, and prefer adding
> it to `base.html` so every feature gets it.

---

## 1. Design tokens (colours)

Defined once in `base.html` `:root`. **Always use the variables — never hard-code a hex** (the only
sanctioned raw hexes are `#fff` on accent/danger buttons and the `#0d1219` input/inset background).

| Token        | Value     | Use |
|--------------|-----------|-----|
| `--bg`       | `#0f1419` | page background |
| `--panel`    | `#1a2330` | panels, sidebar, card headers |
| `--line`     | `#2a3646` | borders, dividers, subtle chips |
| `--fg`       | `#e6edf3` | primary text |
| `--muted`    | `#8b98a8` | secondary text, labels, notes |
| `--accent`   | `#3b82f6` | primary actions, links, active nav |
| `--ok`       | `#16a34a` | success / good / "same or lower" |
| `--warn`     | `#d97706` | warnings |
| `--bad`      | `#dc2626` | errors / destructive |
| `--stock`    | `#0891b2` | stock/info badges |

Inset fields (inputs, selects, code insets) sit on `#0d1219`. Price movement: green (`--ok`) = same
or lower, red (`#e5534b`) = increase.

---

## 2. Buttons

This is where things drift most, so it is the most important section.

### 2.1 The button classes (defined in `base.html`)

All are `<button>` selectors — they style `<button>` elements, **not** `<a>`.

| Class            | Look | When to use |
|------------------|------|-------------|
| `button` (bare)  | solid accent, `margin-top:1.2rem`, `padding:.6rem 1.2rem` | the **primary submit** at the bottom of a form (the top margin spaces it from the last field) |
| `button.ghost`   | same as bare but `margin:0` | an accent button used **inline / in a row** (in a table cell, next to other controls) |
| `button.danger`  | maroon (`#9b2226`), `margin:0` | **destructive / remove-like** actions (delete line, retire) |
| `button.quiet`   | small, transparent, bordered, muted | **low-emphasis utility** (modal close ✕, cancel, log out) |

### 2.2 The action-bar pattern (`.act-bar` / `.act-btn`)

For a **row of page-level action buttons** (the strip under the stat cards on a detail/list page),
use the uniform action bar from the assembly BOM page. Its whole point: **every control in the bar is
the same height (2.5rem) and radius (9px)** regardless of whether it's an `<a>`, `<button>`, or an
`<input>`. This is the fix for buttons that look "out of sync".

```css
.act-bar { display:flex; flex-wrap:wrap; gap:.6rem; align-items:center; margin:.25rem 0 1.25rem; }
.act-btn { display:inline-flex; align-items:center; justify-content:center; height:2.5rem;
           margin:0; padding:0 1.1rem; background:var(--accent); color:#fff; font-weight:600;
           border:0; border-radius:9px; text-decoration:none; white-space:nowrap; cursor:pointer;
           font:inherit; box-sizing:border-box; }
.act-btn:hover { filter:brightness(1.08); }
.act-btn.secondary { background:transparent; border:1px solid var(--line); color:var(--fg); }
.act-btn.secondary:hover { filter:none; border-color:var(--accent); }
```

- Primary action → `.act-btn`. Lower-priority action in the same bar → `.act-btn.secondary`
  (transparent, bordered). `box-sizing:border-box` keeps the bordered one the exact same height.
- `.act-btn` works on both `<a href>` (navigation) and `<button>` (submit) — pick the element by
  what it does, style stays identical.
- **This CSS now lives in `base.html`** (global) — just use `.act-bar` / `.act-btn` /
  `.act-btn.secondary`, no per-page `<style>` block. (The assembly BOM page still carries an
  identical local copy for historical reasons; don't add new local copies.)

### 2.3 Hard rules

- **No text-link actions in tables.** Every row action is a real button. Navigation ("Edit" that
  goes to another page) → `<button type="button" class="ghost" onclick="location.href='…'">Edit</button>`.
  A POST action (retire, delete) → a `<form>` wrapping a `<button type="submit" class="…">`. Plain
  `<a>text</a>` is fine only for a genuine hyperlink (a part number, a breadcrumb, `part ↗`).
- **Never re-style a button with ad-hoc inline `style=` for colour/height/padding.** Use a class. If
  no class fits, add one.
- **Destructive actions confirm.** `onsubmit="return confirm('…')"` on delete/retire/convert.
- Match heights within a group. Two buttons on the same row must be the same height (don't mix
  `ghost` with `quiet` where they'd look mismatched — see §2.1 for which to pick).

---

## 3. Layout & components

- **Panels.** Content lives in `<div class="panel">` (rounded, bordered, `--panel` bg). A page may
  have several stacked panels (see the assembly page: BOM panel + devmgmt panel).
- **Page header order:** breadcrumb (`<p class="note"><a>← Back</a></p>`) → `<h1>` → optional
  `.note` subtitle → `.cards` stat row → `.act-bar` action row → content.
- **Stat cards.** `<div class="cards">` of `<div class="card"><b>value</b><small>LABEL</small></div>`.
  Cards are for **numbers**, not actions — keep action buttons in an `.act-bar`, not jammed into the
  cards row.
- **Tables.** `<table>` inside `<div style="overflow-x:auto;">` so wide tables scroll instead of
  breaking the layout. Numeric columns use `class="num"` (right-aligned, tabular figures). Trailing
  actions column has an empty `<th>` and is gated on `can_edit`; remember to widen the empty-state
  `colspan` when `can_edit`.
- **Badges.** `<span class="badge {ok|warn|bad|stock|muted}">` — uppercase pill labels for
  status/category. Reuse the semantic colour (e.g. `stock` for ASSY, `bad` for retired).
- **Forms.** `<label>` above `<input>`; inputs are full-width on `#0d1219`. Group side-by-side fields
  in a `display:flex; gap` row. Primary submit is a bare `button`; an inline Save/Cancel pair uses
  `ghost` + `quiet`. Errors render in `.err` (or a `.badge.bad` block for a single message).
- **Typeahead / autocomplete.** For a live "type to search" field, use the `.ac-wrap` / `.ac-menu` /
  `.ac-item` primitives in `base.html` with the shared `/static/article-lookup.js` helper
  (`attachArticleLookup({input, prefix, descTarget})`). It progressively enhances a plain text input
  (the field stays free-text), debounces, fetches JSON from a feature endpoint, and supports
  arrow-key + Enter selection. Reuse it for any code/part lookup; don't hand-roll a new dropdown.

---

## 4. Behaviour / accessibility

- **Role-gate in both places.** Show an action only when the user may perform it (`{% if can_edit %}`)
  **and** gate the route (`require_role`). The template hint is not the security boundary.
- **Dark theme only.** The app is a single dark theme; don't add light-mode styling.
- **Keep it LAN-simple.** No external CDNs/fonts — everything is inline in `base.html`. System font
  stack. Don't add a build step or a CSS framework.

---

## 5. Change log (design decisions)

- **2026-07-13** — Established this file. Codified the button system, the `.act-bar`/`.act-btn`
  uniform action bar (from the assembly BOM page), and the "real buttons in table rows, never text
  links" rule while bringing the Article Register in line with the assembly BOM page.
- **2026-07-13** — Promoted `.act-bar` / `.act-btn` / `.act-btn.secondary` from a per-page `<style>`
  block into `base.html` as global primitives (Article Register's template pages were the third
  consumer). Use them directly; don't paste local copies.
- **2026-07-15** — Added a shared typeahead widget: `.ac-wrap` / `.ac-menu` / `.ac-item` CSS in
  `base.html` + `/static/article-lookup.js` (`attachArticleLookup`). First used on the part-number
  field of Add-component and New-assembly to suggest unassigned Article Register numbers (endpoint
  `GET /article-register/api/unassigned`). Reuse it for future code/part lookups.
- **2026-07-17** — Article Register "Add template lines to a family" preview: a template line whose
  group (prefix) is already present in the target family is now **skipped** (not duplicated at a new
  suffix). The preview marks each skipped line with a `badge warn` "already in family" and strikes
  its code (`text-decoration:line-through`, `--muted`). Reuse the strike-through + status-badge
  pattern for any preview row that won't be created.
- **2026-07-19** — New **Documents** feature (`/documents`, nav 📄 order 16). Controlled documents
  (CAD, specs, drawings, compliance/ISO, binaries) carry an Article Register number and keep an
  append-only revision history; source code is stored as an external **link** (GitHub), never a file.
  UI conventions established here, reuse them: a **file/link kind** shown as a `badge stock`
  (file) / `badge warn` (link); the create form uses radio toggles that show either the file input or
  the URL fields, and auto-forces "link" (disabling the file option) when the class is `95` (software)
  — mirror this show/lock pattern for any either/or form. Revision tables use real `.ghost` buttons
  per row ("Download", "Make current"), never text links, and the current revision is tagged
  `badge ok`. The Article Register family page gained a **Documents panel** (rows + an
  `.act-btn.secondary` "New document for this family"); reuse the guarded cross-feature read
  (`repo.list_family_documents`, try/except so the owning feature can be absent) for surfacing one
  feature's rows on another's page.
- **2026-07-19** — Article Register "Add template lines to a family": the skip is now by **exact
  code**, not by whole prefix (this corrects the 2026-07-17 entry above). A template line is struck +
  `badge warn` "already in family" only when its exact number (prefix + running no + the line's own
  suffix) already exists. This lets a template add further suffixes under a prefix that's already
  present — e.g. append `99-…-2` (Stencil TOP) / `99-…-3` (Stencil BOT) to a family that so far only
  has `99-…-1` (PCB). The preview and the server (`apply_template`) both key on the family's existing
  codes (`repo.family_codes`); template suffixes are treated as meaningful document identities and
  honoured as-is.
- **2026-07-19** — "Internal document" flag on parts (`is_document`). Now that the internal article
  register also covers documents, the part form has a document checkbox. Coupling (server is
  authoritative, the form JS mirrors it): a **5x-class part number** → always a document; a
  **document** (5x number *or* ticked box) → always excluded from BOM cost. The form ticks + **locks**
  the forced-downstream box and shows a `badge muted` hint (`5x number` on the document box,
  `documents always excluded` on the exclude box), remembering the user's own choice so it returns
  when the forcing condition is removed. Reuse this tick-lock-hint-remember pattern for any pair of
  checkboxes where one implies the other.
- **2026-07-19** — Lifecycle-owned statuses in forms. A status that is set by an *action* (customer
  order `shipped` by dispatch, `complete` by invoicing the last despatch, `cancelled` by Cancel) is
  never offered in an edit-form dropdown: the form shows it as a fixed `badge muted` + a note ("set
  by despatch / invoicing"), and only the hand-settable statuses (draft/confirmed) remain options.
  Action buttons that require a state are hidden until it holds (Pack & ship appears only on a
  confirmed order). Apply this pattern to any future status field whose transitions carry side
  effects.
- **2026-07-19** — UI-consistency sweep bringing the older list/detail pages in line with this file.
  (1) The **"New X"** button on every list page (Parts, Assemblies, Contacts, Customer/Work/Purchase
  Orders) moved out of the `.cards` stat row into its own `.act-bar` below the cards, as an
  `.act-btn` — no more inline-styled `<a>` jammed in with the numbers (cards are for numbers, actions
  live in an action bar). The Part/Contact detail "Edit" links became `.act-btn` (was inline 7px).
  (2) **No text-link row actions**: `parts_list` value cell is now plain text with a real per-row
  `.ghost` **Edit** button in a trailing `can_add`-gated column; `assembly_import_result` and
  `part_cleanup` no longer use text-link edits (a real button / a part-number hyperlink to the detail
  page respectively). (3) New **`.code`** utility in `base.html` (`font-family:ui-monospace,…`) — use
  it for internal codes (article/part numbers) instead of an inline `font-family`; first adopted on
  the Article Register list + detail. (4) Documents form Title marked `(required)` + `required` attr.
- **2026-07-19** — **Product = the Article Register family; documents are collected under it, not in
  the BOM.** An assembly and its documents/parts share one running number (`98-NNNNN-1` assembly,
  `54-NNNNN-x` drawings, `99-NNNNN-x` parts = family `NNNNN`). Document-class lines are zero-cost
  *deliverables*: they live in the Documents feature (revision history, controlled files/GitHub
  links) and are **never** BOM lines. The assembly detail page now shows a **Documents panel**
  (below the BOM, above devmgmt) listing the family's documents — reached by parsing the running
  number from the assembly's own part number (`_family_no` + guarded `family_documents` read) — with
  a `＋ New document` `.act-btn.secondary` linking to `/documents/new?running_no=NNNNN`. File/link
  kind badge follows the Documents convention (`stock`=file / `warn`=link). When the from-template
  flow creates a product, the new-assembly "created" dialog now lists the documents alongside the
  stub parts so they're never silently created. Reuse this "surface a family's documents by running
  number" read for any other product-scoped view.
  **Split lists, one per thing-kind:** on the Article Register family page a family is shown as two
  tables — **Parts & assemblies** (98/99 lines) and **Documents** (50–59/95 lines) — a document-class
  number appears ONLY under Documents, never in the items table (no more "same number in two lists").
  The Documents table lists *every* document-class family line: materialised → "Edit/Open document";
  allocated-but-empty → a `not created` tag + "Create document". The article-number management actions
  (Edit/Duplicate/Retire/Delete) live alongside each row in whichever table it belongs to. General
  rule: **group a product's rows by kind (physical/BOM vs deliverable), don't interleave them.**
- **2026-07-15** — "Allocate & return" pattern: an inline `ghost` button next to the part-number
  field on Add-component / New-assembly jumps to the Article Register allocator (New Number /
  New Product) carrying a validated `return_to` path; on success the allocator redirects back to the
  create page with `?part_no=<code>` prefilled. Return paths are same-site only (`_safe_return`
  rejects scheme/host/`//`). Reuse `return_to` + a `part_no` prefill param for any allocate-elsewhere
  flow rather than opening a second window.
