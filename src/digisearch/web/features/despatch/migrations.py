"""Despatch + invoicing — shipping goods against a customer order (miniMRP tbldespatch/detail).

Despatching a line posts an ISSUE movement (stock out), consumes any allocation reserved to that
order line, and bumps the order line's shipped_qty. A despatch note can then be marked invoiced.
"""

from __future__ import annotations

from ...core import Migration

MIGRATIONS = [
    Migration(
        version=1,
        name="despatch notes",
        sql="""
        CREATE TABLE despatches (
            id            INTEGER PRIMARY KEY,
            despatch_no   TEXT,                              -- our delivery/despatch note number
            order_id      INTEGER REFERENCES customer_orders(id),   -- CustOrderID
            customer_id   INTEGER REFERENCES contacts(id),   -- DespCustID
            despatch_date TEXT,                              -- DespDate
            advice_no     TEXT,                              -- DespNoteNo (carrier/our note ref)
            status        TEXT NOT NULL DEFAULT 'open',      -- open|invoiced
            invoice_no    TEXT,                              -- InvoiceNo
            invoice_date  TEXT,                              -- Invoiced
            notes         TEXT,
            minimrp_id    INTEGER UNIQUE,                    -- DespID
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX ix_desp_order ON despatches(order_id);
        CREATE INDEX ix_desp_status ON despatches(status);

        CREATE TABLE despatch_lines (
            id            INTEGER PRIMARY KEY,
            despatch_id   INTEGER NOT NULL REFERENCES despatches(id) ON DELETE CASCADE,
            order_line_id INTEGER REFERENCES customer_order_lines(id),  -- OrderDetailRowID
            part_id       INTEGER REFERENCES parts(id),      -- DespStockID
            qty           REAL NOT NULL DEFAULT 0,           -- DespQty
            unit_price    REAL,                              -- InvNetPriceEA (invoice price)
            minimrp_id    INTEGER UNIQUE                     -- DespDetailID
        );
        CREATE INDEX ix_despline_desp ON despatch_lines(despatch_id);
        """,
    ),
    Migration(
        version=2,
        name="despatch invoice error",
        sql="""
        -- Last Fortnox invoicing problem for this despatch (e.g. "awaiting customer confirmation"
        -- or an API error). NULL when invoiced cleanly or not yet attempted; drives a retry/confirm
        -- prompt on the despatch. The Fortnox invoice number itself goes in the existing invoice_no.
        ALTER TABLE despatches ADD COLUMN invoice_error TEXT;
        """,
    ),
    Migration(
        version=3,
        name="packing list workflow",
        sql="""
        -- A despatch now starts life as a PACKING LIST: the operator checks off each line as it's
        -- physically packed, then confirms the package is ready before it can be dispatched. No
        -- stock moves until dispatch. Lifecycle: packing -> packed -> open (dispatched) -> invoiced.
        -- 'packed' = this line has been checked off the packing list.
        ALTER TABLE despatch_lines ADD COLUMN packed INTEGER NOT NULL DEFAULT 0;
        -- When/by whom the package was confirmed ready to ship (the 'packed' transition).
        ALTER TABLE despatches ADD COLUMN packed_at TEXT;
        ALTER TABLE despatches ADD COLUMN packed_by TEXT;
        """,
    ),
]
