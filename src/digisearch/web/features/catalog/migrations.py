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
]
