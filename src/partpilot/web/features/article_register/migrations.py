"""Article Register: Invector's internal part-number allocation authority.

Internal numbers are compound ``PREFIX-NNNNN-S`` (see ``codes.py``). This feature owns two tables:

* ``article_prefixes`` — the reference list of prefixes (category / customer), seeded here from the
  ``Förklaringar`` sheet of the legacy ``Artikelregister`` workbook. Prefix ``01–89`` is the
  customer-product range (each assigned customer gets its own code); ``40`` iLabs ICs; ``50–59``
  internal documents; ``90/95/96/97/98/99`` internal categories.
* ``article_numbers`` — the allocated numbers. A row with a blank prefix/suffix is a *reserved*
  running number (allocated for future use, no category assigned yet). ``code`` is the assembled
  ``PREFIX-NNNNN-S`` and is NULL while reserved. Struck-through numbers in the workbook import as
  ``retired = 1`` and must never be reused.

No FK to catalog ``parts``: the link between a code and a catalog part is a query-time string match
(``parts.part_no = article_numbers.code``), which keeps the register decoupled from the catalog.
"""

from __future__ import annotations

from ...core import Migration

# Seed the prefix reference table from the legacy workbook's explanation sheet.
_PREFIX_SEED = [
    # code, label, category
    ("01", "AddMobile", "customer"),
    ("02", "Alesco", "customer"),
    ("03", "Procode", "customer"),
    ("04", "Invector Embedded Systems", "customer"),
    ("05", "Brainlit", "customer"),
    ("06", "miThings", "customer"),
    ("07", "Svenska Kraftnät", "customer"),
    ("08", "Slice", "customer"),
    ("40", "Integrated circuits developed by iLabs", "ic"),
    ("50", "Marketing documents", "document"),
    ("51", "Reports", "document"),
    ("52", "Instructions", "document"),
    ("53", "Technical reports", "document"),
    ("54", "Drawings / Specifications", "document"),
    ("55", "Legal documents", "document"),
    ("56", "Project documents", "document"),
    ("57", "Production sheets", "document"),
    ("58", "Registers / Lists", "document"),
    ("59", "Quality documents", "document"),
    ("90", "Low-level product definition", "internal"),
    ("95", "Software tied to products", "internal"),
    ("96", "Production tools", "internal"),
    ("97", "Work & other costs", "internal"),
    ("98", "Assemblies", "internal"),
    ("99", "Individual components", "internal"),
]


def _seed_values() -> str:
    def esc(s: str) -> str:
        return s.replace("'", "''")

    return ",\n            ".join(
        f"('{code}', '{esc(label)}', '{category}')" for code, label, category in _PREFIX_SEED
    )


MIGRATIONS = [
    Migration(
        version=1,
        name="article_register",
        sql=f"""
        CREATE TABLE article_prefixes (
            code     TEXT PRIMARY KEY,          -- '01','40','54','98' … (2 chars)
            label    TEXT NOT NULL,             -- 'Brainlit', 'Assemblies', 'Drawings / Specifications'
            category TEXT NOT NULL,             -- customer | document | ic | internal
            active   INTEGER NOT NULL DEFAULT 1
        );

        INSERT INTO article_prefixes (code, label, category) VALUES
            {_seed_values()};

        CREATE TABLE article_numbers (
            id         INTEGER PRIMARY KEY,
            prefix     TEXT,                    -- NULL for a reserved (unassigned) running number
            running_no INTEGER NOT NULL,
            suffix     INTEGER,                 -- NULL for reserved
            code       TEXT,                    -- assembled 'PREFIX-NNNNN-S'; NULL when reserved
            product    TEXT,                    -- description
            created_by TEXT,                    -- initials (LO/PO/TA/JP …)
            comment    TEXT,
            retired    INTEGER NOT NULL DEFAULT 0,
            source     TEXT,                    -- 'excel' | 'manual'
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE UNIQUE INDEX ux_article_code ON article_numbers(code) WHERE code IS NOT NULL;
        CREATE UNIQUE INDEX ux_article_triplet ON article_numbers(prefix, running_no, suffix)
            WHERE prefix IS NOT NULL;
        CREATE INDEX ix_article_running ON article_numbers(running_no);
        CREATE INDEX ix_article_prefix ON article_numbers(prefix);
        """,
    ),
    Migration(
        version=2,
        name="article_templates",
        # Product-structure templates: a named, ordered set of lines that generate a whole product
        # family in one shot. Each line is a (prefix, suffix, label); on apply the code becomes
        # PREFIX-NNNNN-suffix and the description is "<product name> – <label>" (label blank = just
        # the name). Seeds one 'Standard PCB product' template matching the house pattern (assembly +
        # PCB/stencils + drawings) — editable/deletable in the UI.
        sql="""
        CREATE TABLE article_templates (
            id         INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            notes      TEXT,
            active     INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE article_template_lines (
            id          INTEGER PRIMARY KEY,
            template_id INTEGER NOT NULL REFERENCES article_templates(id) ON DELETE CASCADE,
            prefix      TEXT NOT NULL,             -- '54','98','99' …
            suffix      INTEGER NOT NULL DEFAULT 1,
            label       TEXT NOT NULL DEFAULT '',  -- description tail ('PCB','Schematic'); '' = name only
            sort_order  INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX ix_article_tmpl_line ON article_template_lines(template_id, sort_order);

        INSERT INTO article_templates (id, name, notes) VALUES
            (1, 'Standard PCB product', 'Assembly + PCB/stencils + core drawings');
        INSERT INTO article_template_lines (template_id, prefix, suffix, label, sort_order) VALUES
            (1, '98', 1, '',            0),
            (1, '99', 1, 'PCB',         1),
            (1, '99', 2, 'Stencil TOP', 2),
            (1, '99', 3, 'Stencil BOT', 3),
            (1, '54', 1, 'Schematic',   4),
            (1, '54', 2, 'Layout',      5),
            (1, '54', 3, 'Gerber files',6);
        """,
    ),
]
