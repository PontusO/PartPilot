"""Assembly BOM structure — `bom_lines` linking a parent assembly to its children.

Both ends reference ``parts`` (assemblies are parts with kind=ASSY), so subassemblies
and where-used fall out naturally: a child that is itself an ASSY is simply a parent of
other bom_lines. Mirrors miniMRP's ``tblusedin`` (ParentID/ChildID/QtyPer/RefText/LineItemNo).
"""

from __future__ import annotations

from ...core import Migration

MIGRATIONS = [
    Migration(
        version=1,
        name="bom_lines",
        sql="""
        CREATE TABLE bom_lines (
            id         INTEGER PRIMARY KEY,
            parent_id  INTEGER NOT NULL REFERENCES parts(id) ON DELETE CASCADE,
            child_id   INTEGER NOT NULL REFERENCES parts(id),
            qty_per    REAL NOT NULL DEFAULT 1,
            refdes     TEXT,                  -- miniMRP RefText (reference designators)
            line_no    INTEGER,               -- display order (LineItemNo)
            comments   TEXT,
            minimrp_id INTEGER UNIQUE
        );
        CREATE INDEX ix_bom_parent ON bom_lines(parent_id);
        CREATE INDEX ix_bom_child ON bom_lines(child_id);
        """,
    ),
]
