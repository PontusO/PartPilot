"""Purchase orders + goods receiving — the inbound (buy) side of the inventory loop.

Modelled on miniMRP's tblporders/tblpodetail (+ goods receiving as a confirmed action that posts
RECEIVE movements). Suppliers reference the catalog `suppliers` table (where part_suppliers' prices
and supplier part numbers already live); the richer `contacts` address book stays separate for now
(documented seam — unify when supplier terms/contacts are needed on a PO).
"""

from __future__ import annotations

from ...core import Migration

MIGRATIONS = [
    Migration(
        version=1,
        name="purchase orders",
        sql="""
        CREATE TABLE purchase_orders (
            id            INTEGER PRIMARY KEY,
            po_no         TEXT,                              -- our PO number
            supplier_id   INTEGER REFERENCES suppliers(id),  -- POSupID (catalog supplier)
            status        TEXT NOT NULL DEFAULT 'draft',     -- draft|ordered|received|cancelled
            order_date    TEXT,                              -- PODate
            required_date TEXT,                              -- ReqdDate
            currency      TEXT,
            notes         TEXT,
            minimrp_id    INTEGER UNIQUE,                    -- POrderID
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX ix_po_status ON purchase_orders(status);
        CREATE INDEX ix_po_supplier ON purchase_orders(supplier_id);

        CREATE TABLE purchase_order_lines (
            id           INTEGER PRIMARY KEY,
            po_id        INTEGER NOT NULL REFERENCES purchase_orders(id) ON DELETE CASCADE,
            part_id      INTEGER REFERENCES parts(id),       -- POStockID
            supplier_pno TEXT,                               -- POSupPNo
            qty          REAL NOT NULL DEFAULT 0,            -- POQty
            unit_price   REAL,                               -- POPrice (per piece)
            qty_received REAL NOT NULL DEFAULT 0,            -- POReceived
            line_no      INTEGER,
            minimrp_id   INTEGER UNIQUE
        );
        CREATE INDEX ix_poline_po ON purchase_order_lines(po_id);
        """,
    ),
    Migration(
        version=2,
        name="goods receipts",
        sql="""
        -- Each receipt against a PO is recorded as a Goods Received Note (miniMRP tblgrn/detail),
        -- giving an auditable delivery history (with the supplier's advice note). Created by
        -- purchase_orders.repo.receive_po alongside the RECEIVE stock movements; surfaced read-only
        -- by the goods_receipts feature.
        CREATE TABLE goods_receipts (
            id          INTEGER PRIMARY KEY,
            grn_no      TEXT,                              -- our GRN number
            po_id       INTEGER REFERENCES purchase_orders(id),
            supplier_id INTEGER REFERENCES suppliers(id),
            grn_date    TEXT,                              -- GRN_Date
            advice_no   TEXT,                              -- GRN_AdviceNo (supplier delivery note)
            notes       TEXT,
            received_by TEXT,
            minimrp_id  INTEGER UNIQUE,                    -- GRN_ID
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX ix_grn_po ON goods_receipts(po_id);

        CREATE TABLE goods_receipt_lines (
            id         INTEGER PRIMARY KEY,
            grn_id     INTEGER NOT NULL REFERENCES goods_receipts(id) ON DELETE CASCADE,
            po_line_id INTEGER REFERENCES purchase_order_lines(id),  -- PORowID
            part_id    INTEGER REFERENCES parts(id),
            qty        REAL NOT NULL DEFAULT 0,            -- ReceivedQty
            minimrp_id INTEGER UNIQUE                     -- DetailID
        );
        CREATE INDEX ix_grnline_grn ON goods_receipt_lines(grn_id);
        """,
    ),
    Migration(
        version=3,
        name="archived PO documents",
        sql="""
        -- The supplier CSV and PDF PO are generated and frozen when a PO is PLACED, and stored
        -- here as immutable records for ISO retention (they travel inside the backed-up DB).
        CREATE TABLE po_documents (
            id         INTEGER PRIMARY KEY,
            po_id      INTEGER NOT NULL REFERENCES purchase_orders(id) ON DELETE CASCADE,
            kind       TEXT NOT NULL,        -- csv | pdf
            filename   TEXT NOT NULL,
            content    BLOB NOT NULL,
            byte_size  INTEGER,
            placed_by  TEXT,
            placed_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX ix_podoc_po ON po_documents(po_id);
        """,
    ),
    Migration(
        version=4,
        name="goods receipt line price",
        sql="""
        -- The per-piece price actually paid for this delivery line (copied from the PO line at
        -- receipt), so each Goods Received Note is a dated record of what stock cost. Receiving also
        -- overwrites the matching supplier offer's unit price with this value ("last purchase
        -- price"); see purchase_orders.repo.receive_po.
        ALTER TABLE goods_receipt_lines ADD COLUMN unit_price REAL;
        """,
    ),
]
