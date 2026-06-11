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
]
