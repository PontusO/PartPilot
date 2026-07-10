"""Catalog schema — the first feature to own data in the source-of-truth DB.

Modelled on miniMRP's part/supplier/stock tables but normalized and stripped of its
spare ``Text1``/``IFlag2``/``Cust3`` columns. Every table keeps a ``minimrp_id`` so a
one-time import can upsert and a dual-run can diff against the Access DB until trusted.
"""

from __future__ import annotations

from ...core import Migration

MIGRATIONS = [
    Migration(
        version=1,
        name="catalog core tables",
        sql="""
        CREATE TABLE suppliers (
            id         INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            short_name TEXT,
            url        TEXT,
            currency   TEXT,
            minimrp_id INTEGER UNIQUE
        );

        CREATE TABLE parts (
            id            INTEGER PRIMARY KEY,
            part_no       TEXT NOT NULL,              -- miniMRP MasterPNo (canonical id / MPN)
            value         TEXT,                       -- ItemName, e.g. "0u1/16V/10%/0402"
            description   TEXT,
            category      TEXT,
            kind          TEXT NOT NULL DEFAULT 'PART', -- PART or ASSY
            mfr_name      TEXT,
            mfr_pno       TEXT,
            rev           TEXT,
            unit_cost     REAL,                       -- per-piece cost (SEK), miniMRP xCost
            min_qty       REAL NOT NULL DEFAULT 0,    -- reorder point
            total_qty     REAL NOT NULL DEFAULT 0,    -- rolled-up on-hand
            total_alloc   REAL NOT NULL DEFAULT 0,
            total_on_order REAL NOT NULL DEFAULT 0,
            notes         TEXT,
            minimrp_id    INTEGER UNIQUE,
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX ix_parts_part_no ON parts(part_no);
        CREATE INDEX ix_parts_category ON parts(category);

        CREATE TABLE part_suppliers (
            id           INTEGER PRIMARY KEY,
            part_id      INTEGER NOT NULL REFERENCES parts(id) ON DELETE CASCADE,
            supplier_id  INTEGER REFERENCES suppliers(id),
            supplier_pno TEXT,
            price_per_uom REAL,                       -- miniMRP PriceEach (price per qty_per_uom)
            qty_per_uom  REAL NOT NULL DEFAULT 1,     -- reel size; unit price = price_per_uom/qty_per_uom
            moq          REAL,
            lead_time    INTEGER,                     -- days
            is_default   INTEGER NOT NULL DEFAULT 0,
            minimrp_id   INTEGER UNIQUE
        );
        CREATE INDEX ix_partsup_part ON part_suppliers(part_id);

        CREATE TABLE stock_locations (
            id         INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            minimrp_id INTEGER UNIQUE
        );

        CREATE TABLE part_stock (
            id          INTEGER PRIMARY KEY,
            part_id     INTEGER NOT NULL REFERENCES parts(id) ON DELETE CASCADE,
            location_id INTEGER REFERENCES stock_locations(id),
            bin         TEXT,
            on_hand     REAL NOT NULL DEFAULT 0,
            allocated   REAL NOT NULL DEFAULT 0,
            on_order    REAL NOT NULL DEFAULT 0,
            minimrp_id  INTEGER UNIQUE
        );
        CREATE INDEX ix_partstock_part ON part_stock(part_id);
        """,
    ),
    Migration(
        version=2,
        name="stock movement ledger",
        sql="""
        -- Every quantity change writes one row here (miniMRP's tblitemhistory). All stock
        -- mutations go through catalog.stock.post_movement so on-hand and this ledger stay
        -- in lock-step. Work orders (ISSUE/BUILD) and goods receiving (RECEIVE) post here too.
        CREATE TABLE stock_movements (
            id          INTEGER PRIMARY KEY,
            part_id     INTEGER NOT NULL REFERENCES parts(id) ON DELETE CASCADE,
            location_id INTEGER REFERENCES stock_locations(id),
            moved_at    TEXT NOT NULL DEFAULT (datetime('now')),
            mtype       TEXT NOT NULL,        -- RECEIVE | ISSUE | BUILD | ADJUST | OPENING
            qty_delta   REAL NOT NULL,        -- signed change applied to on-hand
            qty_after   REAL NOT NULL,        -- part's total on-hand after the move (running balance)
            reference   TEXT,                 -- source doc: WO/PO/GRN number or "manual"
            note        TEXT,
            user        TEXT
        );
        CREATE INDEX ix_stockmove_part ON stock_movements(part_id, moved_at);
        """,
    ),
    Migration(
        version=3,
        name="assembly default build days",
        sql="""
        -- Default build length (WORKING days) for an assembly; seeds work-order auto-planning.
        -- Null for components (only meaningful on kind='ASSY').
        ALTER TABLE parts ADD COLUMN default_build_days INTEGER;
        """,
    ),
    Migration(
        version=4,
        name="webshop sync baseline",
        sql="""
        -- The part's stock quantity in the WooCommerce webshop as of the last sync (the
        -- reconcile baseline). NULL = never synced; the first sync adopts Woo's value. Lets the
        -- two-way sync tell webshop sales (Woo went down) from production (PartPilot went up).
        ALTER TABLE parts ADD COLUMN webshop_synced_qty REAL;
        """,
    ),
    Migration(
        version=5,
        name="external webshop price",
        sql="""
        -- The part's price as advertised on the WooCommerce webshop, copied verbatim by the sync.
        -- Deliberately separate from the calculated cost (unit_cost): it's the customer-facing shop
        -- price, kept for book-keeping / inventory valuation reports. NULL until first synced.
        ALTER TABLE parts ADD COLUMN external_price REAL;
        """,
    ),
    Migration(
        version=6,
        name="unlimited-stock parts",
        sql="""
        -- Flags a part as having unlimited stock that never runs out: used for non-physical
        -- "parts" like SMT Assembly or other labour/service lines that sit in an assembly's BOM
        -- purely to carry a cost into the rolled-up product cost. Such parts are never short, never
        -- below their reorder point, and are not consumed from stock when a work order is issued.
        ALTER TABLE parts ADD COLUMN unlimited_stock INTEGER NOT NULL DEFAULT 0;
        """,
    ),
    Migration(
        version=7,
        name="distinct webshop-sale movement type",
        sql="""
        -- Webshop sales used to be logged as generic ISSUE movements (stock out). They now get
        -- their own movement type, WOOSALE, so the ledger/report can tell a Woo sale apart from a
        -- work-order issue or a despatch shipment. Backfill historical rows by their sync
        -- reference ('woo-sale') so old and new sales read consistently. mtype is a display/filter
        -- label only — nothing branches on it — so re-tagging is safe.
        UPDATE stock_movements SET mtype = 'WOOSALE'
         WHERE mtype = 'ISSUE' AND reference = 'woo-sale';
        """,
    ),
    Migration(
        version=8,
        name="devmgmt device catalog + build records",
        sql="""
        -- The product-catalog layer PartPilot pushes to devmgmt (docs/partpilot-integration.md).
        -- Sits ABOVE the parts/assemblies tables: a variant layers radio + firmware metadata onto
        -- an existing buildable assembly (parts.kind='ASSY'). All `ref` values are opaque, stable,
        -- PartPilot-controlled strings that both sides key on and never reuse. JSON-shaped columns
        -- (radio_capabilities, enabled_radios, radio_config, radios) are stored as TEXT and
        -- (de)serialized in the repo — SQLite has no native array/object type.

        CREATE TABLE product_models (
            id                 INTEGER PRIMARY KEY,
            ref                TEXT NOT NULL UNIQUE,        -- shared model ref, e.g. "PM-CONN840"
            name               TEXT NOT NULL,
            radio_capabilities TEXT NOT NULL DEFAULT '[]', -- JSON array, e.g. ["ble","lorawan"]
            created_at         TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at         TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE board_revisions (
            id       INTEGER PRIMARY KEY,
            model_id INTEGER NOT NULL REFERENCES product_models(id) ON DELETE CASCADE,
            ref      TEXT NOT NULL UNIQUE,                  -- shared board-rev ref, e.g. "PM-CONN840-C"
            rev      TEXT NOT NULL,                         -- human label, e.g. "C"
            UNIQUE (model_id, rev)
        );
        CREATE INDEX ix_boardrev_model ON board_revisions(model_id);

        CREATE TABLE variants (
            id             INTEGER PRIMARY KEY,
            ref            TEXT NOT NULL UNIQUE,            -- shared variant ref, e.g. "SKU-CONN840-WEBSHOP"
            model_id       INTEGER NOT NULL REFERENCES product_models(id) ON DELETE CASCADE,
            assembly_id    INTEGER REFERENCES parts(id),   -- the buildable BOM (parts.kind='ASSY')
            sku            TEXT NOT NULL UNIQUE,            -- human SKU string; the catalog join key
            enabled_radios TEXT NOT NULL DEFAULT '[]',     -- JSON array, subset of the model's radios
            radio_config   TEXT,                           -- JSON object or NULL
            created_at     TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX ix_variants_model ON variants(model_id);

        -- Per-component factory firmware a variant ships with (docs §5.2). update_method is a
        -- property of the component on this board that devmgmt projects verbatim.
        CREATE TABLE variant_flashable_targets (
            id                   INTEGER PRIMARY KEY,
            variant_id           INTEGER NOT NULL REFERENCES variants(id) ON DELETE CASCADE,
            component            TEXT NOT NULL,             -- e.g. "mcu", "lte_modem", "wifi_module"
            factory_firmware_ref TEXT NOT NULL,             -- e.g. "MCU-CONN840-1.2.0"
            update_method        TEXT NOT NULL
                CHECK (update_method IN ('ota_via_mcu', 'local_serial', 'local_usb')),
            line_no              INTEGER                    -- display / push order
        );
        CREATE INDEX ix_flashtarget_variant ON variant_flashable_targets(variant_id);

        -- One row per manufactured unit (docs §5.3). owner_token is stored in PLAINTEXT: PartPilot
        -- is the issuer and needs it to (re)generate the device QR/label; devmgmt stores only its
        -- hash. `radios` is a JSON array of {tech, identity{}, secrets{}} captured at test-station
        -- provision time. work_order_id is a soft link (no FK — work_orders is a later-registered
        -- feature, so a real FK would invert migration order). pushed_at records the last
        -- successful devmgmt push; NULL = not yet pushed.
        CREATE TABLE device_builds (
            id            INTEGER PRIMARY KEY,
            serial        TEXT NOT NULL UNIQUE,            -- globally unique; the devmgmt handle
            variant_id    INTEGER NOT NULL REFERENCES variants(id),
            board_rev     TEXT NOT NULL,                  -- rev label, must exist on the model
            owner_token   TEXT NOT NULL,                  -- high-entropy, per device (plaintext here)
            radios        TEXT NOT NULL DEFAULT '[]',     -- JSON array of radio identities + secrets
            work_order_id INTEGER,                        -- soft link to work_orders(id); optional
            pushed_at     TEXT,                           -- last successful devmgmt push (ISO), or NULL
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX ix_devicebuilds_variant ON device_builds(variant_id);
        """,
    ),
    Migration(
        version=9,
        name="devmgmt push outbox",
        sql="""
        -- Transactional outbox for the devmgmt auto-triggers. Catalog edits (model/variant upserts)
        -- and work-order completion enqueue a row here IN THE SAME TRANSACTION as the change, so the
        -- intent to push is durable even if devmgmt is down; a background loop (devmgmt_sync) drains
        -- it with retry. Keeping the network call out of the request path means a WO can always be
        -- finished even when devmgmt is unreachable. `ref` is the model.ref / variant.ref /
        -- device.serial to push; UNIQUE(kind, ref) coalesces repeated edits of the same object into
        -- one pending job (re-enqueue resets it to pending with a fresh attempt count).
        CREATE TABLE devmgmt_outbox (
            id          INTEGER PRIMARY KEY,
            kind        TEXT NOT NULL,          -- 'model' | 'variant' | 'device'
            ref         TEXT NOT NULL,          -- model.ref / variant.ref / device.serial
            status      TEXT NOT NULL DEFAULT 'pending',  -- pending | done | error
            attempts    INTEGER NOT NULL DEFAULT 0,
            last_error  TEXT,
            enqueued_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (kind, ref)
        );
        CREATE INDEX ix_devmgmt_outbox_status ON devmgmt_outbox(status);
        """,
    ),
    Migration(
        version=10,
        name="devmgmt retire lifecycle",
        sql="""
        -- Soft-retire timestamps for the active -> retired -> deleted lifecycle (docs §7). A
        -- non-null retired_at means the model/variant is retired: PartPilot hides it from default
        -- listings and pushes "retired": true so devmgmt hides it too, but it stays resolvable for
        -- devices that already reference it. NULL = active. Hard delete (guarded: retire first, no
        -- references) removes the row entirely and is propagated via a DELETE outbox job.
        ALTER TABLE product_models ADD COLUMN retired_at TEXT;
        ALTER TABLE variants        ADD COLUMN retired_at TEXT;
        """,
    ),
    Migration(
        version=11,
        name="devmgmt outbox retry backoff + generation counter",
        sql="""
        -- next_attempt_at: earliest time a pending job is due again (NULL = due now). Written with
        -- exponential backoff on transient failures so an outage is probed at a decaying rate
        -- instead of every tick, and jobs are never permanently abandoned while devmgmt is down.
        -- seq: bumped on every (re-)enqueue of the same (kind, ref); the flusher's status updates
        -- are guarded on it so completing an in-flight push can't clobber a concurrent re-enqueue
        -- (the push used pre-edit data — the row must stay pending so the edit is re-sent).
        ALTER TABLE devmgmt_outbox ADD COLUMN next_attempt_at TEXT;
        ALTER TABLE devmgmt_outbox ADD COLUMN seq INTEGER NOT NULL DEFAULT 0;
        """,
    ),
]
