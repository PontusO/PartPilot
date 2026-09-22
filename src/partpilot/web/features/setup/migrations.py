"""Setup-owned schema: a small key/value app-settings store.

Company details (for the PDF purchase order / ISO records) live here under ``company.*`` keys,
edited via Setup & Tools → Company details and read by the PO export.
"""

from __future__ import annotations

from ...core import Migration

MIGRATIONS = [
    Migration(
        version=1,
        name="app settings",
        sql="""
        CREATE TABLE app_settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        """,
    ),
]
