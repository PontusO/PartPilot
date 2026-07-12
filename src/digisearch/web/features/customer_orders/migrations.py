"""Customer orders — header + lines, modelled on miniMRP's tblcustorders/detail.

Allocation (tblallocdetail) and despatch/invoice (tbldespatch/detail) are deliberately
left for later layers; ``shipped_qty`` and ``minimrp_id`` are kept so those slot in cleanly.
"""

from __future__ import annotations

from ...core import Migration

MIGRATIONS = [
    Migration(
        version=1,
        name="customer orders",
        sql="""
        CREATE TABLE customer_orders (
            id              INTEGER PRIMARY KEY,
            order_ref       TEXT,                              -- our order number
            customer_id     INTEGER REFERENCES contacts(id),  -- CustID -> a customer contact
            customer_po     TEXT,                              -- CustPONo
            status          TEXT NOT NULL DEFAULT 'draft',     -- draft|confirmed|shipped|complete|cancelled
            order_date      TEXT,                              -- CustORDate (ISO date)
            required_date   TEXT,                              -- ReqdDate (due date)
            currency        TEXT,
            discount_rate   REAL,                              -- order-level CODiscountRate (%)
            delivery_charge REAL,                              -- DelCharge
            tax_rate        REAL,                              -- COTaxRate (%)
            notes           TEXT,                              -- COComment
            minimrp_id      INTEGER UNIQUE,                    -- CustORID (for a future import)
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX ix_custorders_customer ON customer_orders(customer_id);
        CREATE INDEX ix_custorders_status ON customer_orders(status);

        CREATE TABLE customer_order_lines (
            id               INTEGER PRIMARY KEY,
            order_id         INTEGER NOT NULL REFERENCES customer_orders(id) ON DELETE CASCADE,
            part_id          INTEGER REFERENCES parts(id),     -- StockID -> product ordered
            line_no          INTEGER,                          -- LineNum
            ordered_qty      REAL NOT NULL DEFAULT 0,          -- OrderedQty
            unit_price       REAL,                             -- StdPriceEA
            discount_percent REAL,                             -- DiscountPercent
            shipped_qty      REAL NOT NULL DEFAULT 0,          -- ShippedQty (despatch later)
            notes            TEXT,
            minimrp_id       INTEGER UNIQUE                    -- RowID
        );
        CREATE INDEX ix_colines_order ON customer_order_lines(order_id);
        """,
    ),
    Migration(
        version=2,
        name="stock allocation",
        sql="""
        -- Reserve on-hand stock to a customer-order line (miniMRP's tblallocdetail). The sum of a
        -- part's allocations is mirrored onto parts.total_alloc, so free = total_qty − total_alloc
        -- becomes real everywhere (availability, build & purchase shortage calcs). Despatch consumes
        -- allocations; cancelling/releasing frees them.
        CREATE TABLE allocations (
            id                     INTEGER PRIMARY KEY,
            customer_order_line_id INTEGER NOT NULL REFERENCES customer_order_lines(id) ON DELETE CASCADE,
            part_id                INTEGER NOT NULL REFERENCES parts(id),
            qty                    REAL NOT NULL DEFAULT 0,
            created_at             TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX ix_alloc_line ON allocations(customer_order_line_id);
        CREATE INDEX ix_alloc_part ON allocations(part_id);
        """,
    ),
    Migration(
        version=3,
        name="backfill customer order refs",
        sql="""
        -- Bring existing orders without a reference onto the CO-NNNNN convention (new orders are
        -- auto-numbered in repo.create_order). Custom refs the operator typed are left untouched.
        UPDATE customer_orders SET order_ref = printf('CO-%05d', id)
        WHERE order_ref IS NULL OR order_ref = '';
        """,
    ),
    Migration(
        version=4,
        name="archived order acknowledgements",
        sql="""
        -- The order-acknowledgement PDF sent to the customer is generated and frozen when the order
        -- is acknowledged, and stored here as an immutable record for ISO retention (it travels
        -- inside the backed-up DB). Re-acknowledging after an amendment appends a new version; all
        -- versions are kept. Mirrors purchase_orders' po_documents.
        CREATE TABLE co_documents (
            id         INTEGER PRIMARY KEY,
            order_id   INTEGER NOT NULL REFERENCES customer_orders(id) ON DELETE CASCADE,
            kind       TEXT NOT NULL,        -- pdf
            filename   TEXT NOT NULL,
            content    BLOB NOT NULL,
            byte_size  INTEGER,
            created_by TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX ix_codoc_order ON co_documents(order_id);
        """,
    ),
    Migration(
        version=5,
        name="order delivery/invoice addresses",
        sql="""
        -- Which of the customer's structured addresses this order ships to / invoices to. Default
        -- from the customer's defaults at create time; overridable per order. Nullable → falls back
        -- to the customer's base address on documents.
        ALTER TABLE customer_orders ADD COLUMN delivery_address_id INTEGER REFERENCES contact_addresses(id);
        ALTER TABLE customer_orders ADD COLUMN invoice_address_id  INTEGER REFERENCES contact_addresses(id);
        """,
    ),
    Migration(
        version=6,
        name="order line price override flag",
        sql="""
        -- Order lines now default their unit_price from the product's tiered SELL price at the
        -- ordered quantity (see catalog/pricing.py). Once an operator types a price by hand we must
        -- stop re-pricing it on a qty change — this flag records that the price was set manually.
        -- A "reprice" action clears it and recomputes from the current tiers.
        ALTER TABLE customer_order_lines ADD COLUMN price_overridden INTEGER NOT NULL DEFAULT 0;
        """,
    ),
]
