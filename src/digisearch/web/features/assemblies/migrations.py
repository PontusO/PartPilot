"""Assembly BOM structure — `bom_lines` linking a parent assembly to its children.

Both ends reference ``parts`` (assemblies are parts with kind=ASSY), so subassemblies
and where-used fall out naturally: a child that is itself an ASSY is simply a parent of
other bom_lines. Mirrors miniMRP's ``tblusedin`` (ParentID/ChildID/QtyPer/RefText/LineItemNo).
"""

from __future__ import annotations

from ...core import Migration

# One-time seed for the catalog `normally_stocked` flag (column added by catalog migration v13).
# Marks every part that appears anywhere in the BOM tree of a product whose part_no starts with
# 90- or 98- (our own products live at those prefixes on different levels). Walks bom_lines
# recursively; the products themselves (assemblies) are not marked. Lives here — not in catalog —
# because it reads `bom_lines`, which this feature owns and creates after catalog's migrations run.
# Kept as a module constant so a test can exercise the exact SQL the migration runs.
SEED_NORMALLY_STOCKED_SQL = """
WITH RECURSIVE comps(id) AS (
    SELECT b.child_id FROM bom_lines b
      JOIN parts p ON p.id = b.parent_id
     WHERE p.part_no LIKE '90-%' OR p.part_no LIKE '98-%'
    UNION
    SELECT b.child_id FROM bom_lines b JOIN comps c ON b.parent_id = c.id
)
UPDATE parts SET normally_stocked = 1 WHERE id IN (SELECT id FROM comps);
"""

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
    Migration(
        version=2,
        name="seed normally_stocked from 90-/98- products",
        # Runs once, after catalog v13 has added parts.normally_stocked and after bom_lines exists.
        # NOTE: products imported later won't auto-seed their parts — those are curated by hand.
        sql=SEED_NORMALLY_STOCKED_SQL,
    ),
]
