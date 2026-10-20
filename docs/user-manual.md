# PartPilot user manual

This manual explains how to run the company's day-to-day work in PartPilot: keeping the parts
catalog and assemblies, taking customer orders, planning and building, buying parts, receiving
goods, despatching and invoicing. It is organised by the jobs you do rather than by screen. Button
and field names are quoted exactly as they appear on screen.

PartPilot is the system of record. What is in it is what we have, owe and have promised.

## Getting started

### Signing in

Open PartPilot in a browser on the office network and sign in with your **Username** and
**Password**. Each person has their own account. If you see "Invalid username or password" after
being away for a while, your account may have been deactivated; ask an admin.

The left-hand sidebar is the menu. It highlights the section you are in and shows your name and
role at the bottom, with **"Change password"** and **"Log out"**. **"Change password"** asks for
your current password and the new one twice.

### The home page

Signing in lands on the home page. Under the title, a grid of equal-width **stat cards**, four to a
row, shows a live count for each section you can open: **Parts**, **Documents**, **Assemblies**,
**Customer orders open**, **Work orders open**, **Purchase orders on order**, **Suppliers** and
**Customers**. The count is a link to that list; the Parts card also notes how many parts are below
their minimum stock. Retired parts and assemblies are not counted or listed.

Each card then lists **New** — the four most recently created records — and **Edited** — the four
most recently changed records, each with the date and a link to its page. Parts and assemblies show
their part number followed by the description (or the value when there is no description), documents
their number and title, purchase orders their number and supplier, work orders their number and
the customer they are built for (a stock build shows the number alone). Customer orders are coloured by status: blue while in progress (Draft,
Confirmed, Shipped), green when Complete, red when Cancelled. A record counts as edited
only when it was changed after it was created; a stock adjustment or a version release counts as a
change to a part. Records created on the day the catalog was first loaded into PartPilot carry that
day as their creation date.

Below the cards, **Latest changes** lists the two most recent changes to the software with their
date and short build id, and the line underneath shows the PartPilot version, the build id and the
versions of Python, SQLite, FastAPI, Starlette and uvicorn it runs on. This is what to quote when
reporting a problem.

### Roles

Every signed-in user can read every screen (except Setup & Tools). What you can *change* depends on
your group, which an admin sets under Setup → Users:

| Group | Can do |
|---|---|
| shipping | Read everything, and despatch: packing lists, ship, invoice. |
| warehouse | Shipping's rights, plus stock adjustments, receiving goods, allocating stock to orders, work orders and the planning calendar. |
| purchasing | Warehouse's rights, plus master data (parts, assemblies, contacts, article numbers, documents), customer orders, purchase orders, shelves for customers and the Purchasing BOM resolver. |
| admin | Everything, including Setup & Tools, user management, deleting records and renaming or retiring shelves. |

If you press a button your group may not use, you get the message "Your role (…) is not permitted
to do that."

### Finding things

Every list page has a **Search** box that matches the obvious fields (part number, value,
manufacturer part number, order reference, customer, supplier) and a **"Filter"** button. A
**clear** link appears while a filter is active. Lists show their key figures as cards at the top
and sort newest first, except Parts, which sorts by category.

### Reference numbers

PartPilot numbers its own records with a prefix and five digits: `CO-00042` for a customer order,
`WO-00017` work order, `PO-00012` purchase order, `GRN-00003` goods received note, `DN-00009`
despatch note, `OA-<order ref>` order acknowledgement. You can type your own order or PO reference
instead when creating one; a blank field takes the next number.

### The two things to understand first

**Shelves.** Stock sits on shelves, and every shelf has an owner. Our own shelf is called **Main**.
A **customer shelf** holds parts a customer has paid for. Our totals, valuation and webshop figures
count only our shelves, and a work order draws from exactly one shelf. See *Stock and shelves*.

**Loaded cost.** Components are never sold on their own. A part's **material cost** is what we pay
the supplier; its **loaded cost** is material × an overhead factor (default 1.30) covering
handling, storage and procurement. Loaded cost is what a customer is charged for the parts in a
build. Production work and profit are priced outside PartPilot on a separate `97-` article.
## Parts

**Parts** is the catalog of everything we stock, buy or cost: components, PCBs, stencils, and
cost-only lines such as assembly labour. Assemblies live on their own page (see *Assemblies*).
Admin and purchasing can add and edit parts.

### The parts list

The cards show how many parts there are, how many categories, how many are **below min** (free
stock under the reorder point) and how many are **normally stocked**. Filter by text, by
**Category**, or tick **"Normally stocked only"**. Each row shows free and on-hand stock (our
shelves only), the reorder point, the default supplier with its part number and unit price, and
the webshop price if the part is sold online. A part with several versions appears once, with a
**+N ver** badge.

### The part page

The part page shows the stock cards (**free**, **on hand**, **allocated**, **min**, **unit cost**),
the details, the suppliers, captured **Supplier cost tiers** (quantity breaks from the
distributor), **Loaded cost tiers**, **Stock by shelf**, the **Adjust stock** form and the last
fifty **Stock movements**. Badges mark special parts: **Normally stocked**, **∞ unlimited stock**,
**document**, **no BOM cost**.

### Adding or editing a part

Press **"+ Add component"** on the list or **"Edit"** on a part.

- **Part number** is the manufacturer part number for bought parts, or an internal article number
  for our own items. On a new part, **"＋ New number"** jumps to the Article Register, allocates a
  `99-` number and brings you back with it filled in. Typing in the field also suggests allocated
  but unused article numbers.
- **Category** is free text and is saved in upper case. Pick an existing one from the list where
  possible so filtering stays useful.
- **Value / spec** uses the iLabs value notation: fields separated by `/`, for example
  `0u1/16V/10%/0402` or `4K7/1%/0.1W/0603`. Press **"⚙ Build…"** to compose it from separate
  fields for a resistor, capacitor or inductor. The BOM resolver and importer use this notation, so
  a complete value makes the part findable when a BOM asks for "0.1uF 0402".
- **Reorder point (min qty)** drives the **below min** figure. It is independent of **Normally
  stocked**, which simply marks parts we deliberately keep.
- **Unlimited stock** is for cost-only lines (SMT assembly, labour). They are never short and are
  not consumed by a work order.
- **Internal document** marks a deliverable such as a drawing. A `5x-` or `95-` part number forces
  it on. Documents are always **excluded from BOM cost**. New document-class numbers should be
  created under Documents, not here.
- **Exclude from BOM cost** keeps a line (a stencil, say) on the BOM without counting its price.

**Opening stock** on a new part books the quantity onto one of our shelves as an OPENING movement.
On an existing part the same section adjusts our stock with an ADJUST movement; customer-shelf
stock is never touched from the form.

**Suppliers.** Add one row per source: **Supplier** (pick one, or "+ New supplier…"), **Supplier
P/N**, **Unit price** per piece, **Reel/UOM qty**, **MOQ**, **Lead (days)**, and tick **Default**.
The default supplier's unit price becomes the part's unit cost and is what purchase suggestions
use. A supplier added here becomes a supplier contact with only a name; complete its address in
Contacts.

**Internal loaded cost.** Leave **Overhead factor** blank to use the Setup default. Manual
**Loaded cost tiers** override generated ones at the same break quantity.

Two buttons at the bottom of the edit page keep prices current:

- **"↻ Refresh cost tiers from distributor"** re-queries Digi-Key or Mouser by supplier part number
  and replaces the captured quantity breaks. The flat unit price is not changed.
- **"Generate loaded cost from material × overhead"** rebuilds the generated loaded tiers from the
  saved overhead factor and the default supplier's breaks. Save a changed factor first.

Loaded tiers are also re-anchored automatically whenever a purchase order line is priced.

### Versions

The **Versions** panel on a part or assembly lets several revisions coexist. **"＋ New version"**
clones the current one as a **Draft** with a new rev: BOM, suppliers and pricing are copied, stock
starts empty. **"Release"** makes a version **Current** and the previous one **Superseded**. New
BOMs, orders and work orders default to the current version; a superseded version stays available
for spares and repairs and can be re-released to roll back. Lists show one row per version group.

### Cleaning up distributor order codes

Older parts may carry a Digi-Key or Mouser order code as their part number. Setup → **Part-number
cleanup** lists them, looks up the real manufacturer part number and promotes it, moving the order
code to the supplier row. See *Setup & Tools*.

### Retiring and deleting a part

A part is never deleted straight away. First **"Retire"** it (admin or purchasing): the part leaves
the Parts list and every picker, is never suggested for purchase, and the BOM resolver no longer
counts its stock. Everything that already references it keeps working: existing stock can still be
issued or despatched, old orders and work orders still show it, and an assembly whose BOM lists it
still builds and consumes it (the BOM line shows a **retired** badge). Tick **"Show retired"** on
the Parts list to see retired parts; a retired part carries a **Retired** badge and an
**"Unretire"** button. Retiring applies to every version of the part.

Only an admin can **"Delete"** a part, and only once it is retired. Deleting removes every version
of the part together with its supplier offers, price tiers and stock rows. It is refused while
anything historical still points at the part: work orders or their lines, customer order lines
and allocations, despatch lines, purchase order or goods receipt lines, units still on hand, stock
movements, another assembly's BOM, or a devmgmt variant. The refusal lists what blocks it, for
example *"Cannot delete R-1: 3 work orders, 12 stock movements, used in 2 assemblies."* In practice
a part that has ever been built, bought or sold can only be retired; delete is for parts created by
mistake. A retired webshop product keeps syncing its stock with the shop, so the last units can
still sell.

## Stock and shelves

### What a shelf is

A shelf is a stock location with an owner. There are two kinds:

- **Our shelves** (badge *ours*), normally just **Main**. Everything on them is ours, counts in
  the part totals, in valuation and in the webshop stock.
- **Customer shelves** (badge *customer*) hold parts a customer has paid for up front. They never
  count as our stock. Only that customer's work orders draw from them, and that material is not
  charged on the build.

A customer can have one general shelf (used for all their jobs) and any number of **job shelves**
tied to a single customer order. Parts left over after a build stay on the shelf; nothing is
transferred automatically.

Setup & Tools → **"Stock shelves"** lists every shelf with owner, order, part count and on-hand.
The same page is linked from every shelf name in the app. A shelf page shows its contents with
**Free** per part, its **value at unit cost**, and the last hundred movements, with an **"Adjust"**
button per line.

### Creating shelves

- **Customer shelf.** On the customer's contact page, section **Stock shelves**, optionally type a
  name and press **"New shelf"**. It is named after the customer by default.
- **Job shelf.** On the customer order page, press **"Create job shelf"**. It is named
  "<customer> · <order ref>" and preselected when you build or buy for that order.
- **Our own shelf.** Admins can add a second own shelf on the Stock shelves page
  (**"New shelf of our own"**). Names are unique across all shelves.

### Adjusting stock

On a part page, **Adjust stock** takes an **Action** (**"Receive (+)"**, **"Issue (−)"** or
**"Set on-hand to…"**), a **Quantity**, the **Shelf** and a **Note**. **"Set on-hand to…"** applies
to the selected shelf only, so a stock count on a customer shelf never changes Main. Every
adjustment is a movement in the ledger with your name on it. Admin, purchasing and warehouse can
adjust.

Stock otherwise moves only through the business actions: receiving a purchase order, issuing and
finishing a work order, despatching, and the webshop sync.

### Reading the stock figures

- **On hand** is what is physically on a shelf. **Allocated** is reserved to customer orders.
  **Free** is on hand minus allocated.
- A part's headline figures and the Parts list count **our shelves only**. The part page prints
  "Ours: N · Customer-owned: M" above the per-shelf table.
- A shelf can go negative when a work order is issued short. The figure shows in red; receive the
  missing parts and the shelf comes right.

### Retiring a shelf

Admins can **"Rename"** or **"Retire shelf"** on the shelf page. A shelf can be retired only when it
is empty, no open purchase order receives into it and no open work order builds from it. The last
own shelf cannot be retired. A retired shelf disappears from every picker but stays in the ledger.
## Assemblies

An assembly is a part of kind ASSY with a bill of materials (BOM). It is what work orders build
and what customer orders usually sell. Admin and purchasing can edit assemblies.

### Creating an assembly

**Assemblies → "New Assy"**. Enter the **Part number** (an internal `98-` number), **Revision**,
**Name / value**, **Category**, **Default build days** and **Description**. Two buttons help with
the number:

- **"＋ New product"** allocates a new product family in the Article Register and returns here.
- **"＋ From template"** creates a whole family from a template (assembly, PCB, stencils,
  schematic, layout, gerbers…) and returns. Stub parts created this way are added to the BOM at
  quantity 1 when you press **"Create assembly"**.

### The assembly page

The cards show **BOM lines**, **used in**, **on hand**, **material cost** and **loaded build cost**.
Change **Build vol** to re-price the page at a different build volume: tier prices are chosen by
the total quantity the build consumes. No distributor is queried for this.

The BOM table lists each line with quantity, the component, its **Unit** and **Line** cost (white =
material, grey bold = loaded) and reference designators. Sub-assemblies are linked and their cost
rolls up. A line with the **no BOM cost** badge is shown but not counted.

Actions:

- **"Add Component to Assy"** opens a picker. Set **Qty** and **Reference designators**, then click
  the part to add it. Only current versions are offered.
- **"Edit"** and **"Remove"** on each line.
- **"Import BOM (.csv)"**: see *Importing a BOM*.
- **"↻ Refresh prices & estimate"** re-queries Digi-Key and Mouser for every component the build
  is short of and shows the refreshed material and loaded figures beside the stored ones, green
  when the price is the same or lower and red when it rose. Components covered by our free stock
  are kept at their stored price and reported as "In stock, kept".
- **"Export BOM (.xlsx)"** downloads the priced BOM at the current build volume.
- **"Convert to component"** turns an empty assembly back into a plain part.
- **"Retire"**, **"Unretire"** and **"Delete"**: see *Retiring and deleting an assembly*.

Below the BOM, the **Documents** panel lists the controlled documents of the product family
(drawings, specs, software) with **"＋ New document"**, and the **Device management (devmgmt)**
panel publishes the product to the device-management server for firmware hosting and claiming.
devmgmt is used only for connected products; ask before publishing.

### Importing a BOM

From the assembly press **"Import BOM (.csv)"**, choose the **BOM file** (CSV or Excel) and the
shelf to check stock on, then **"Resolve"**. Each line is classified and matched (see *How the BOM
resolver works*) and the review screen shows:

A line whose only catalog match is a **retired** part shows the **retired** badge with no tick box: it
is neither linked nor created, and the result page lists it under **Not added — retired parts** so
you can unretire the part or add a replacement by hand.

| Status | Meaning | What happens |
|---|---|---|
| in catalog | A part we already have, matched by part number or manufacturer part number. | Linked automatically. Shows how many are free on the chosen shelf. |
| new | Found at Digi-Key or Mouser but not in our catalog. | Pre-ticked; will be created with supplier, price, quantity breaks, manufacturer and the guessed category. |
| unresolved | Nothing found, but the line has a value or part number. | Tick to create it bare from the BOM text, then complete it by hand. |
| manual | Too under-specified to resolve. | Cannot be imported. Fix the BOM or add the part yourself. |
| skip | Do-not-populate or non-orderable (test points, fiducials). | Left out. |

A BOM column headed **Manufacturer** (also Mfr, Mfg, Maker, Brand or Tillverkare) is picked up
automatically: its name becomes the created part's manufacturer, overriding the distributor's name.
The **Category** column shows the category each new or unresolved row will be created with. It is
guessed from the BOM text for passives and crystals, else from the distributor's category for the
chosen part, else from the RefDes prefix (R, C, L, U, D, Q, J, SW, F, ANT and so on). A dash means no
rule matched and the part is created without a category; correct either on the part page afterwards.
The rules live in `config/category_rules.yaml`.
The **iLabs value** column shows the value the importer composed; a **⚠ N** badge means fields are
missing. Press **"Add to assembly"**. Ticked parts are created and every row with a part becomes a
BOM line. If any created part has an incomplete value, a result page lists them under **Needs
checking** with an **"Edit ↗"** button each.

The BOM file's columns are recognised by name (refdes, qty, value, device, package, description,
comment and common variants). If a file has unusual headers, use Setup → **Import parts from BOM**
instead, which lets you map columns and can wrap the result into a new assembly.

### Versions of an assembly

Versions work as for parts (see *Parts → Versions*): **"＋ New version"** clones the BOM,
suppliers and pricing into a draft; **"Release"** makes it current. Work orders and orders already
raised stay pinned to the version they were created against.

### Retiring and deleting an assembly

Assemblies follow the part rules (see *Parts → Retiring and deleting a part*). **"Retire"** (admin
or purchasing) removes the assembly from the Assemblies list, the **New Work Order** picker and the
customer-order product picker, and refuses new work orders for it; finished boards on the shelf can
still be despatched. Tick **"Show retired"** on the Assemblies list to find it again, and
**"Unretire"** brings it back. **"Delete"** (admin only, after retiring) removes the assembly and
its own BOM lines; it is refused while any work order, order line, stock movement, parent BOM or
devmgmt variant references it.

## Customer orders

A customer order is the commercial record: who ordered what, at which price, and by when. Everything
downstream, the builds, the purchasing and the despatch, hangs off it. Admin and purchasing create
and edit orders; every role can read them.

### Creating an order

1. Open **Customer Orders** and press **"New Order"**. If the customer is not in Contacts yet, the
   form tells you so and links to **Contacts → New customer**; add them first.
2. Choose the **Customer** (required). The **Deliver to** and **Invoice to** selectors fill with
   the customer's addresses, with their defaults preselected. Leave them on "— customer default —"
   unless this order goes somewhere else.
3. **Our order ref** is pre-filled with the next `CO-NNNNN`. Keep it, or type your own
   (for example `SO-1042`). **Customer PO no.** is the customer's own reference and is printed on
   the acknowledgement.
4. Set **Order date** (defaults to today) and **Required date**. The planning calendar at the
   bottom of the form shows the builds already committed; clicking a day copies it into the
   Required date.
5. Optional commercial terms: **Currency**, **Order discount %**, **Delivery charge**,
   **Tax rate %** and **Notes**.
6. Press **"Create order"**. New orders start as *draft* unless you set *confirmed* on the form.

### Adding products

On the order page press **"Add Product"**. Filter by part number or value, click the product, set
**Qty**, **Unit price** and **Disc %**, then **"Add to order"**. Only the current released
version of each part or assembly is offered.

- Leave **Unit price** blank for the automatic tiered price at the ordered quantity. The order
  line then follows the costing model: the loaded cost (material × overhead) rolled up through
  the BOM. Production and profit are priced as a separate `97-` part, so add that line too when
  you quote a build.
- Type a price to pin it. The line shows an **override** badge; **"Reprice"** on the row drops the
  override and recomputes the tiered price.
- Lines can be edited inline (Qty, Unit price, Disc %, then **"Save"**) or removed with
  **"Remove"**. Lines stay editable in every status, so take care on orders that are already
  shipped.

The footer shows **Goods subtotal**, the order discount, delivery, tax and **Order total**.
Tax is applied to goods minus discount plus delivery.

### Order status

| Status | How it is set |
|---|---|
| draft | Default on creation. Can be set to confirmed on the edit form. |
| confirmed | Set on the edit form, or by **"Acknowledge order"** (see below). Only confirmed orders can be despatched. |
| shipped | Set by despatch, when every line is fully shipped. A partial despatch leaves the order confirmed. |
| complete | Set by invoicing, once the order is shipped and every despatch is invoiced. |
| cancelled | Set by **"Cancel order"**. |

Shipped, complete and cancelled are shown as fixed badges on the edit form. They are set by
actions, never by hand.

### Acknowledging an order

**"Acknowledge order"** generates an order acknowledgement PDF (`OA-<ref>.pdf`) and stores it
for ISO records. With the default Setup → Order settings, acknowledging a draft order also
confirms it. The order needs at least one line first. **"Preview acknowledgement"** shows the
live document before anything is stored; afterwards the button becomes **"Re-issue
acknowledgement"** and **"Download acknowledgement"** fetches the latest stored version. Every
issued version is kept in the database; the table at the bottom of the order lists them.

### Reserving stock

**"Allocate stock"** reserves free stock to the order's lines so that other orders and builds see
it as taken. It walks the shelves in this order and takes what is free on each: the order's
**job shelf**, then the customer's other **customer shelves**, then **our shelves** (Main first).
Another customer's shelf is never used. Run it again at any time to top up; **"Release
allocation"** frees everything again. Cancelling the order also releases its allocations.

A work order in *Planned* state does **not** reserve stock. Allocation is the only reservation.

### When stock is short

If an order line is an assembly that cannot be filled from stock, the order page shows
"⚠ N line(s) can't be filled from stock" with a **"Create work orders"** button. Press it, or
**"Check stock & build"**, to go to the build proposal (see *Building to an order* below).
Short components, as opposed to assemblies, are bought, not built: see *Buying*.

### Cancelling an order

**"Cancel order"** is available while an order is draft or confirmed. It releases reserved stock
but does **not** cancel work orders or purchase orders raised for it. The cancelled order shows a
warning listing those documents so you can review them by hand.

## Work orders

A work order (WO) is one batch of one assembly, built from one shelf. Admin, purchasing and
warehouse can create and progress work orders.

### Status names

| Shown as | Meaning |
|---|---|
| Planned | Created and scheduled. The BOM has been exploded and snapshotted. Nothing has moved or been reserved. |
| In WIP | Components have been issued out of stock. |
| Finished | Finished assemblies have been booked into stock. |
| Cancelled | Dropped before anything moved. |

### Building to an order

From the customer order press **"Check stock & build"**. The page lists every ordered line with
**Ordered**, **Free stock** (summed over the job shelf, the customer's shelves and ours),
**On WO** (already planned or in WIP) and **Shortfall**. Assembly lines with a shortfall are
pre-ticked with **Build qty** set to the shortfall. Components are marked "component → purchase"
and are never built.

An order line for a **retired** assembly is shown with a **retired** badge and no tick box: it cannot
be built, only despatched from stock. If a retired assembly is submitted anyway, nothing is created
and the page explains why.

Choose **"Build from shelf"**. The picker offers our shelves and this customer's shelves, and
preselects the job shelf if one exists, otherwise the customer's shelf, otherwise Main. If the
customer has no shelf and the parts are being bought for them, press **"Create customer shelf"**
first (admin and purchasing only), then create the work orders.

**"Create N work order(s)"** creates one WO per ticked line, linked to the order line, due on the
order's Required date, drawing from the chosen shelf.

### Creating a stand-alone work order

**Work Orders → "New Work Order"**. Choose the **Assembly to build** (only active assemblies with
a BOM are offered; a retired assembly cannot be built), **Build quantity**, the **Shelf** components are issued from, **Planned finish**
and **Build days (working)**. Build days default from the assembly. **Work order no.** is
assigned as `WO-NNNNN` if left blank. The shelf cannot be changed after creation.

### What the work order page shows

- The component requirements, exploded to base parts across sub-assemblies, with the spillage
  margin from Setup → Production settings already added. The margin in force is printed in the
  table heading and frozen onto this WO.
- **Available on shelf** is the free quantity on the WO's own shelf. Stock on other shelves does
  not count, even if Main has plenty. **Short** is what is missing on that shelf.
- The **loaded build cost**. On a customer shelf the material is customer-supplied, so the
  charged cost is **0.00** and the card shows the reference value instead. The badge
  **"customer-owned parts"** marks such work orders.
- The schedule: **Planned start** is back-calculated from the planned finish over working days.
  If it ends later than the customer asked for, a warning says so; the customer's date is kept
  on the order.
- An "Order materials by" banner when any component has a supplier lead time, with
  **"Review & raise POs"** leading to the purchase suggestions.

### Progressing a work order

- **"Issue components (→ WIP)"** issues every required quantity out of the WO's shelf. A short
  shelf goes negative; the red "component(s) short" badge warns you but does not block.
  Cost-only lines such as assembly labour are skipped.
- **"Finish (→ stock)"** books the built quantity onto the same shelf and sets the build date.
- **"Flush (issue + finish)"** does both in one step for builds that are already done.
- **"Cancel"** is only possible while Planned.
- **"Reschedule"** changes the planned finish or build days; start and the purchasing deadline
  follow.

### When the BOM changes after planning

If the assembly's BOM is edited while a WO is Planned, the list and the WO page show
"⚠ BOM changed". **"Regenerate BOM"** rebuilds the component lines from the current BOM and the
current spillage setting, keeping the dates. Work orders in WIP or finished are not affected.

## Planning

**Planning** is a calendar of all work orders with a start date. Each purple bar runs from the
planned start to the due date; green bars are finished builds. The blue marker is the
"order materials by" date; it turns red when overdue. Weekends are hidden.

Admin, purchasing and warehouse can drag:

- Drag a bar to move the whole build. The due date and the purchasing deadline follow.
- Resize a bar to change the run length.
- Drag the blue marker on its own to start sourcing earlier. Moving the bar afterwards
  recomputes the marker.

Clicking a bar opens the work order; clicking a marker opens the purchase suggestions. The
customer's requested date is kept on the order and is not shown here.

## Despatch

Despatch ships a confirmed customer order in one or more deliveries. All four roles can despatch.
Nothing leaves stock until the final **"Despatch now"** step.

### Pack & ship

1. On a **confirmed** order press **"Pack & ship"**. Draft orders must be confirmed first.
2. The page lists **Ordered**, **Shipped**, **Outstanding**, **Reserved** and **In stock** per
   line, with **Ship now** pre-filled to what can be shipped. Untick lines or lower the quantity
   for a partial delivery.
3. **"Create packing list"** opens a packing list `DN-NNNNN` in status *Packing*. Nothing has
   moved yet.
4. Tick **Packed** as you pack each item and **"Save progress"**. When every item is ticked,
   **"Confirm package ready to ship"** sets *Ready to ship*. **"Reopen packing"** goes back.
5. **"Despatch now (ship & issue stock)"** ships the goods. This is the step that issues stock.

**"Cancel packing list"** discards a list before despatch; no stock has moved.

### Where the goods come from

Stock is issued from the shelves recorded on the order's allocations. Any unallocated remainder is
taken from the customer's job shelf, then their customer shelf, then our shelves, and finally
Main if nothing is left. Lines that are reserved on a customer's own shelf carry the badge
**"ships from customer shelf"** on the packing list.

After despatch the order's shipped quantities are updated. When every line is fully shipped the
order becomes *shipped*; a partial delivery leaves it *confirmed* so another despatch can follow.

### Invoicing

A despatched note shows an **Invoice** section:

- With Fortnox connected, **"Create Fortnox invoice"** creates one draft invoice per despatch.
  If the customer is not in Fortnox yet, the page previews what will be created and offers
  **"Create customer & invoice"**. Errors are stored and can be retried with
  **"Retry Fortnox invoice"**. Despatch also tries this automatically.
- Without Fortnox, expand **"Record an invoice number manually instead"**, enter **Invoice no.**
  and **Invoice date**, and press **"Mark invoiced"**.

The despatch becomes *Invoiced*. Once the order is shipped and every despatch is invoiced the
order becomes *complete*.
## Buying

There are two ways to buy. **Purchase orders** cover shortages for planned work orders, part by
part, from the suppliers on our parts. **Purchasing** (the BOM resolver) takes a whole BOM file,
finds each line at Digi-Key or Mouser and produces cart files you upload to the distributor. Admin
and purchasing can do both; warehouse can also receive goods.

### Purchase suggestions from shortages

When a work order is **Planned**, its components become demand. **Purchase Orders** shows the
banner "⚠ N component(s) are short for your allocated work orders" with **"Review & raise POs"**.
The suggestions page lists every part that is short, grouped by **shelf** and then by supplier:

- **Short** = required by planned work orders on that shelf, minus **Free on shelf**, minus what is
  already **On order** into that shelf.
- A customer shelf shows its own shortages even when Main has the part, because a work order
  draws only from its shelf.
- Parts with no supplier are listed under **No supplier** and cannot be ordered until you add one
  on the part.
- Cost-only parts (unlimited stock) are never suggested.
- A **retired** part is never suggested, even when a work order is short of it: retired means run
  out, don't re-buy.

Untick lines or whole suppliers, adjust **Order qty**, and press **"Create draft purchase
orders"**. One draft PO is created per supplier per shelf, receiving into that shelf. Distributor
lines are re-priced live from the current quantity breaks, and those breaks are saved on the part.

### Raising a purchase order by hand

**Purchase Orders → "New PO"**: choose the **Supplier** and the **Receive into shelf** (Main
unless you are buying for a customer), optionally a **PO number**, dates, **Currency** and
**Notes**, then **"Create purchase order"**. On the PO page use **"Add Part"**: click the part, set
**Qty**, leave **Unit price** blank for the tier price (re-priced live for Digi-Key and Mouser
parts) or type one. Lines can be edited with **"Save"** or removed while the PO is a draft. The
shelf cannot be changed afterwards.

### Placing the order

**"Place order"** moves the PO from *draft* to *ordered* and archives the supplier CSV and the PDF
as the official copies for ISO records. Nothing is sent automatically: download **"⬇ Supplier
CSV"** to upload to the distributor's cart and **"⬇ PDF PO"** to email to a supplier. The PDF
carries our company details from Setup, the supplier's address from Contacts, the PO number, dates,
currency, the receive-into shelf, and every line with supplier part number, manufacturer part number, quantity and price.

| Status | Meaning |
|---|---|
| draft | Being prepared. Lines editable. |
| ordered | Placed with the supplier. Partly received POs stay here. |
| received | Everything has arrived. Set automatically. |
| cancelled | Dropped but kept on record. |

**"Cancel PO"** keeps the record. **"Delete PO"** removes it entirely and is only possible while
nothing has been received against it; use it for orders that never happened.

### Receiving goods

Open the *ordered* PO. The **Receive goods** section lists outstanding lines with **Receive now**
pre-filled to the outstanding quantity. Enter what actually arrived (less for a partial delivery, 0
to skip a line), optionally the **Supplier advice / delivery note no.**, and press **"Receive into
stock"**. Each receipt:

- creates a Goods Received Note `GRN-NNNNN` with the receiver's name, downloadable as PDF or CSV;
- books every line onto the PO's receive-into shelf as a RECEIVE movement;
- records the PO price as the supplier's last purchase price on the part (the part's unit cost used
  for costing is left unchanged);
- closes the PO when nothing is outstanding.

Over-receiving is refused: "receiving X exceeds the outstanding N". If a PO points at a shelf that
has since been retired it cannot be received; cancel and recreate it. **Goods Receipts** lists all
notes, searchable by GRN, advice number, supplier or PO. A GRN cannot be edited or reversed; correct
mistakes with a stock adjustment.

### Purchasing: resolving a whole BOM

**Purchasing** takes a BOM file and produces distributor carts. Upload the **BOM file** (CSV or
Excel), set the **Build quantity (boards)**, choose the shelf under **Check stock on shelf**, and
press **"Resolve & build carts"**. This can take a minute while Digi-Key is queried.

The result page shows each line with its status, the chosen supplier and manufacturer part number,
the **Order P/N** to buy, packaging, **Buy** quantity, unit and line cost and a **Conf**idence
percentage. Downloads:

- **"Report (Excel)"**: the full resolution with notes.
- **"Digi-Key cart"** and **"Mouser cart"**: CSV files for the distributors' list upload. Only
  confidently resolved lines go in.
- **"Review cart"**: lines that need a human decision before ordering.

PartPilot never places an order. Upload the cart, check it, and order at the distributor.

### Choosing the stock shelf

All three upload screens (Purchasing, assembly Import BOM, Setup Import parts) share the
**Check stock on shelf** selector:

- **Main** (default): lines covered by our free stock are marked *in stock* and not bought.
- **A customer's shelf**: only that customer's stock counts. Parts we hold on Main are still priced
  and bought, which is what you want when the customer pays for their own parts.
- **"Don't check stock"**: every line is priced at the distributors.

### How the BOM resolver works

Each BOM line is classified and then matched:

1. **Do-not-populate** values (DNP, DNM, NF…) and **non-orderable** items (test points, fiducials,
   mounting holes) are listed but not bought.
2. **Generic passives** such as `0.1uF 0402` or `4K7 0603` are matched by value and package. The
   resolver assumes sensible defaults (X7R or C0G dielectric, 16 V, 1 % tolerance) and flags the
   line for review so you can confirm. A value outside the standard E-series is flagged as a
   possible BOM error.
3. **Crystals** are matched by frequency and package size.
4. Anything that looks like a **manufacturer part number** is searched exactly, then with relaxed
   queries; a relaxed match is flagged "nearest match, verify".
5. Lines with no usable value or part number are **manual**.

Our own stock is checked first on the chosen shelf. Then Digi-Key is searched; Mouser is consulted
only when Digi-Key has no good match or is out of stock, and wins only if it scores better.

| Status | Meaning |
|---|---|
| resolved | Confident pick, in stock. Goes into the cart. |
| in stock | Covered by free stock on the chosen shelf. Nothing to buy. |
| review | A part was picked but needs checking (low confidence, assumed specs, out of stock, nearest match). Goes to the review cart. |
| not found | Nothing matched. |
| manual | Under-specified. A person must identify the part. |
| dnp / non orderable | Listed, not priced. |

**Reels.** If a full reel costs less than the reel threshold (10 000 SEK by default), the resolver
orders whole reels and notes "extra to stock"; otherwise cut tape for the exact quantity.

**API budget.** Digi-Key allows roughly a thousand searches a day. Results are cached for seven
days, so re-running the same BOM is free. Avoid resolving very large BOMs repeatedly in one day.

### Bulk-loading parts from a file

Setup → **"Import parts from BOM"** (admin) is for part lists with unusual column layouts, or for
loading parts without attaching them to an assembly. Upload, then **map columns**: for each file
column pick the PartPilot field it feeds or leave it as ignore. At least **Value** or
**Device / MPN** must be mapped. Map a **Manufacturer** column when the file has one: its name is
written to each created part's manufacturer field and wins over the name Digi-Key or Mouser
reports, and parts created bare from unresolved rows get it too. The review page's **Category**
column works as for an assembly import. The mapping is remembered for files with the same header
layout.
**"Resolve rows"** runs the resolver; the review and result pages work as for an assembly import.
On the result page, **Wrap into an assembly** creates a new assembly whose BOM is every imported row
that has a catalog part.
## Contacts

**Contacts** is the address book: **customer**, **supplier** and **other**. Admin and purchasing
can edit. The list filters by **Kind**; supplier rows have a **"Parts ↗"** pill that lists every
part sourced from them.

### Customers

A customer contact is needed before an order can be raised. The contact page holds the registered
address and a set of **Delivery & invoice addresses**, each with its own company name, label,
address and phone, flagged **default delivery** or **default invoice**. Orders pre-fill from the
defaults and let you pick another. Add them with **"+ Add address"**.

The **Stock shelves** section lists the customer's shelves and creates new ones (see *Stock and
shelves*). **Fortnox customer #** appears once the customer has been invoiced through Fortnox.

### Suppliers

A supplier is a contact of kind *supplier*, and this is the only place supplier details live. The
supplier dropdowns on parts and purchase orders list these contacts, and the purchase order PDF
prints the address, organisation number, email and phone from here. Fill in **Currency** so new
purchase orders default to it, and **Discount %** if it applies. A contact that is used as a
supplier on any part, purchase order or goods receipt cannot be changed to another kind; the form
says so. There is no delete; mark unused contacts in **Notes**.

## Article Register

The Article Register allocates our internal part numbers. Admin and purchasing can allocate, edit
and retire; only admin can delete.

### The number format

A number is `PREFIX-NNNNN-S`:

- **Prefix** (two digits) is the class, chosen from a group. `98` assemblies, `99` components,
  `97` work and other costs, `96` production tools, `95` software, `90` low-level product
  definitions, `50`–`59` document classes (54 drawings and specifications, 57 production sheets,
  59 quality documents, and so on), `40` iLabs integrated circuits, `01`–`08` customer products.
- **Running number** (five digits) identifies the **product family**. Everything belonging to one
  product shares it: `98-00400-1` the assembly, `99-00400-1` its PCB, `54-00400-1` its schematic.
- **Suffix** distinguishes variants within a prefix and family.

The next running number is the lowest free one. Retired and reserved numbers stay taken forever.
A number's identity cannot be changed once allocated; only its description, creator and comment.

### Allocating numbers

- **"New Product"** creates one running number with one line per group you tick, all suffix 1.
  Document-class lines get a stub in Documents at the same time.
- **"New from template"** creates a whole family from a template such as "Standard PCB product"
  (assembly, PCB, stencils, schematic, layout, gerbers). **"Templates"** manages them.
- **"New Number"** allocates a single number, either in a new family or added to an existing
  running number. The preview shows the code as you type.
- Entering **Add component** or **New assembly** from the catalog side offers the same actions and
  returns with the number filled in.

The **family page** (`Running number NNNNN`) lists the family in two tables, **Parts & assemblies**
and **Documents**, with **"Create part"** or **"Create document"** where the catalog record does
not exist yet, **"Edit"**, **"Duplicate"** (next free suffix), **"Retire"**/**"Restore"**, and
**"Apply template"** to add a template's lines to an existing family.

**Retire** is the normal way to take a number out of use; it is reversible and the number stays
taken. **Delete** is for genuine mistakes and is irreversible.

## Documents

**Documents** holds controlled documents against article numbers: drawings, specifications,
production sheets, compliance records and software. Admin and purchasing can create and revise;
admin can delete. Files are stored on the server's disk, not in the database, so they are backed
up separately.

### Creating a document

**"New document"**: pick the **Document class**, choose **New number** or an **Existing running
no.** to join a product family, give it a **Title** and optional **Type** tag, and choose the
storage:

- **Upload a file**: any type, up to 100 MB.
- **External link**: a URL, optionally a branch or tag and a path. Class 95 (software) is always a
  link to the repository, never a copied file.

The first **Revision** defaults to A. From the Article Register family page, **"Create document"**
binds the document to a number that already exists.

### Revisions

Revisions are append-only. On a file document, **Upload new revision** adds a file and makes it
current; older revisions stay downloadable. On a link document, **Update link** records a new link
revision. **"Make current"** promotes an older revision again. The number, class and storage kind
are fixed; **"Edit"** changes only title, type and notes.

**"Retire"** hides a document from the default lists and is reversible. Retiring a document does
not retire its article number.
## Reports

**Reports** are read-only views. The **Stock movement ledger** lists every quantity change with a
running balance per shelf. Filter by **From** and **To** date (the last 30 days by default),
**Type** and **Shelf**, then **"Apply"**.

| Type | Label | Source |
|---|---|---|
| RECEIVE | Goods in | Receiving a purchase order, manual receive. |
| ISSUE | Issue / shipped | Work order issue, despatch, manual issue. |
| BUILD | Build | Finished assemblies booked by a work order. |
| ADJUST | Adjustment | Manual adjustments, part edits, webshop corrections. |
| WOOSALE | Webshop sale | Sales pulled from the webshop. |
| OPENING | Opening balance | Opening stock on a new part. |

The **Balance** column is that shelf's on-hand after the movement, and the **Shelf** column marks
customer shelves. The cards at the top summarise the whole filtered range. The table shows at most
2 000 rows; narrow the dates if you hit the cap.

## Setup & Tools

Admin only. Each tool is a tile on the Setup & Tools page.

- **Import parts from BOM.** Bulk-load parts from any CSV or Excel list with column mapping. See
  *Buying → Bulk-loading parts from a file*.
- **Stock shelves.** All shelves, ours and customers'. See *Stock and shelves*.
- **Part-number cleanup.** Finds parts whose part number is really a distributor order code. Tick
  them, press **"🔎 Look up selected"** to fetch the manufacturer part number from Digi-Key or
  Mouser (uses API quota), check the result, then **"Apply selected"**. Both the part number and
  the manufacturer part number are set; the order code stays on the supplier row.
- **Company details.** Name, address, VAT and organisation numbers, contact details. Printed on
  purchase orders, goods received notes and order acknowledgements.
- **Production settings.** **Spillage / scrap margin (%)** and **Minimum margin (pcs per
  component)**. Every new work order adds the larger of the two to each component requirement,
  rounded up. Existing work orders keep the margin they were created with unless you regenerate
  their BOM.
- **Order settings.** **"Acknowledging a customer order also confirms it"** (on by default). Turn it
  off if orders should be confirmed by hand after the acknowledgement goes out.
- **Pricing settings.** The **Default overhead factor (material → loaded cost)**, 1.30 by default.
  A part's own factor overrides it. This is overhead, not profit.
- **Users.** Add people with **"Add user"** (username, full name, password, group). Change a user's
  group from the dropdown, **"Reset"** a password, **"Deactivate"** an account that should no
  longer sign in (its name stays on past records) and **"Reactivate"** it later. **"Delete"** works
  only for accounts that never signed in. You cannot change or deactivate your own account, nor the
  last admin.
- **Webshop settings** and **Sync webshop.** Connect the WooCommerce store with a Read/Write API
  key and optionally a **Daily automatic sync time**. **"Preview (no changes)"** shows what a sync
  would do; **"Apply sync"** pulls sales into PartPilot as WOOSALE movements, creates missing
  products (SKU `99-` components, `98-` assemblies), and writes reconciled quantities back to the
  shop. The automatic daily run only pulls. The first sync of a product takes the webshop's
  quantity as the baseline. Movements land on our default shelf.
- **Fortnox.** Save the **Client ID**, **Client secret** and **Redirect URL** from the Fortnox
  developer portal, set the **Default VAT %** and optional sales account, then **"Connect to
  Fortnox"**. Once connected, despatching creates one draft invoice per despatch for the accountant
  to finish. **"Disconnect"** stops this.

## Frequently asked questions

**A part shows plenty on hand but the work order says it is short.** The work order draws from one
shelf only. Check which shelf the WO is on (shown under its title) and where the stock actually is
(**Stock by shelf** on the part page). Either move the parts with two adjustments, or if the
customer should be buying their own, raise the PO into their shelf.

**The customer's parts are counted in our stock.** They were received onto Main. Issue them from
Main and receive them onto the customer's shelf with **Adjust stock**, with a note explaining why.

**"Only a confirmed order can be despatched".** Open the order and either set **Status** to
confirmed on **"Edit"** or press **"Acknowledge order"**.

**I cannot delete a purchase order.** Goods have been received against it. **"Cancel PO"** instead;
the record is kept.

**The Purchasing page says my role cannot run purchasing.** Only admin and purchasing can run the
resolver. Ask an admin to change your group.

**Resolving a BOM fails with a Digi-Key error.** The daily search budget may be exhausted or the
credentials missing. Try again the next day; repeated runs of the same BOM come from the cache and
do not count.

**A work order was planned, then the BOM changed.** The WO shows "⚠ BOM changed". Press
**"Regenerate BOM"** before issuing; it rebuilds the component lines and keeps the schedule.

**The cancelled order still shows work orders and POs.** Cancelling an order never cancels what was
raised for it. Review the listed documents and cancel them by hand if they are no longer needed.

**Where are the PDFs kept?** Order acknowledgements and placed purchase orders are archived in the
database and downloadable from the order or PO page. Goods received notes are generated on demand.
Document files live in the documents folder on the server.

**Something looks half-updated after a change to PartPilot.** Templates reload instantly but the
server code does not; the service needs a restart. Tell whoever maintains the server.
