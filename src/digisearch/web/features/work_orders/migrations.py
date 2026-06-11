"""Work orders — build an assembly, consuming components and producing finished stock.

Modelled on miniMRP's tblworders. The BOM is **fully exploded to base components** at creation
and snapshotted into work_order_lines, so later BOM edits never change a live work order.
Lifecycle (miniMRP's three states): allocated -> issued (WIP) -> finished, plus cancelled.
"""

from __future__ import annotations

from ...core import Migration

MIGRATIONS = [
    Migration(
        version=1,
        name="work orders",
        sql="""
        CREATE TABLE work_orders (
            id           INTEGER PRIMARY KEY,
            wo_no        TEXT,                              -- our work-order number
            assembly_id  INTEGER NOT NULL REFERENCES parts(id),   -- WOAssyID (kind=ASSY)
            qty          REAL NOT NULL DEFAULT 1,           -- WOQty to build
            status       TEXT NOT NULL DEFAULT 'allocated', -- allocated|issued|finished|cancelled
            customer_order_line_id INTEGER REFERENCES customer_order_lines(id),  -- optional build-to-order link
            location_id  INTEGER REFERENCES stock_locations(id),
            build_date   TEXT,
            notes        TEXT,
            minimrp_id   INTEGER UNIQUE,                    -- WOrderID
            created_at   TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX ix_workorders_status ON work_orders(status);
        CREATE INDEX ix_workorders_assembly ON work_orders(assembly_id);

        CREATE TABLE work_order_lines (
            id            INTEGER PRIMARY KEY,
            work_order_id INTEGER NOT NULL REFERENCES work_orders(id) ON DELETE CASCADE,
            part_id       INTEGER NOT NULL REFERENCES parts(id),   -- a base component
            qty_required  REAL NOT NULL DEFAULT 0,        -- exploded qty for the whole WO
            qty_issued    REAL NOT NULL DEFAULT 0,
            line_no       INTEGER
        );
        CREATE INDEX ix_wolines_wo ON work_order_lines(work_order_id);
        """,
    ),
    Migration(
        version=2,
        name="work order scheduling",
        sql="""
        -- Planning-calendar dates (ISO strings). Back-scheduled from the customer due date using
        -- the assembly's build duration; null while a WO is unscheduled (it just won't show on the board).
        ALTER TABLE work_orders ADD COLUMN planned_start TEXT;   -- back-scheduled build start
        ALTER TABLE work_orders ADD COLUMN due_date      TEXT;   -- planned finish (seeds from customer required_date)
        ALTER TABLE work_orders ADD COLUMN duration_days INTEGER; -- build length in WORKING days
        """,
    ),
    Migration(
        version=3,
        name="work order purchasing date",
        sql="""
        -- The planned 'order materials by' date. Defaults to the critical lead-time date when the
        -- WO is (re)planned, but is draggable on the calendar for hard-to-source parts.
        ALTER TABLE work_orders ADD COLUMN purchase_by TEXT;
        """,
    ),
    Migration(
        version=4,
        name="work order spillage margin",
        sql="""
        -- The production spillage/scrap % in effect when the WO was created, snapshotted here so
        -- changing the global setting doesn't retroactively alter existing batches.
        ALTER TABLE work_orders ADD COLUMN spillage_percent REAL;
        """,
    ),
    Migration(
        version=5,
        name="work order minimum margin",
        sql="""
        -- Minimum spillage margin (whole parts) per component, snapshotted alongside spillage_percent.
        ALTER TABLE work_orders ADD COLUMN min_margin_qty REAL;
        """,
    ),
]
