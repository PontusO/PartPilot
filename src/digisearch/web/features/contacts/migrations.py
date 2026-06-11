"""Contacts: a unified address book for suppliers, customers and other companies.

miniMRP keeps these in three separate tables (tblsupaddresses / tblcusaddresses /
tblmisaddresses) with an identical schema; we fold them into one ``contacts`` table with a
``kind`` discriminator. ``minimrp_id`` (AddID) is only unique *within* each source table, so
the upsert key is (source, minimrp_id).
"""

from __future__ import annotations

from ...core import Migration

MIGRATIONS = [
    Migration(
        version=1,
        name="contacts",
        sql="""
        CREATE TABLE contacts (
            id          INTEGER PRIMARY KEY,
            kind        TEXT NOT NULL DEFAULT 'supplier',   -- supplier | customer | other
            name        TEXT NOT NULL,                      -- CoName
            short_name  TEXT,                               -- ShortNm
            contact     TEXT,                               -- Contact1 (person)
            email       TEXT,
            phone       TEXT,                               -- Tel1
            phone2      TEXT,                               -- Tel2
            fax         TEXT,
            address     TEXT,                               -- Add1..Add5 joined
            postcode    TEXT,                               -- PCode
            website     TEXT,                               -- URL
            currency    TEXT,                               -- defCurrency
            discount    REAL,
            notes       TEXT,                               -- Comment
            minimrp_id  INTEGER,
            source      TEXT,                               -- sup | cus | mis (origin table)
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (source, minimrp_id)
        );
        CREATE INDEX ix_contacts_kind ON contacts(kind);
        CREATE INDEX ix_contacts_name ON contacts(name);
        """,
    ),
]
