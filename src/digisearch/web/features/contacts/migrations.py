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
    Migration(
        version=2,
        name="structured delivery/invoice addresses",
        sql="""
        -- A contact's registered/general address stays on contacts (freeform address + postcode,
        -- now plus a country). Customers that ship/invoice to distinct places (often under different
        -- trading names, sometimes several sites) get structured rows here, each tagged for delivery
        -- and/or invoice use with one default of each. Added in PartPilot and never touched by a
        -- miniMRP re-import (which only upserts the base contact).
        ALTER TABLE contacts ADD COLUMN country TEXT;

        CREATE TABLE contact_addresses (
            id                  INTEGER PRIMARY KEY,
            contact_id          INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
            label               TEXT,        -- "Head office", "Malmo plant"
            company             TEXT,        -- trading name for this address (may differ from contact.name)
            contact             TEXT,        -- person at this site
            line1               TEXT,
            line2               TEXT,
            city                TEXT,
            region              TEXT,        -- state / province / county
            postcode            TEXT,
            country             TEXT,
            phone               TEXT,
            email               TEXT,
            is_delivery         INTEGER NOT NULL DEFAULT 0,
            is_invoice          INTEGER NOT NULL DEFAULT 0,
            is_default_delivery INTEGER NOT NULL DEFAULT 0,
            is_default_invoice  INTEGER NOT NULL DEFAULT 0,
            created_at          TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX ix_ctaddr_contact ON contact_addresses(contact_id);
        """,
    ),
    Migration(
        version=3,
        name="contact org number + fortnox link",
        sql="""
        -- Organisation / VAT registration number, used to match (and, with confirmation, create)
        -- the matching customer in Fortnox so we never duplicate customers there.
        ALTER TABLE contacts ADD COLUMN org_no TEXT;
        -- The linked Fortnox CustomerNumber once matched/created; reused on later invoices.
        ALTER TABLE contacts ADD COLUMN fortnox_customer_number TEXT;
        """,
    ),
]
