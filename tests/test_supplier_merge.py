"""Dropping the catalog `suppliers` table: contacts (kind=supplier) become the only supplier record."""

import dataclasses

import pytest

from partpilot.web.core import FeatureRegistry
from partpilot.web.core.db import Database, table_exists
from partpilot.web.features.assemblies import feature as asm_feature
from partpilot.web.features.catalog import feature as catalog_feature
from partpilot.web.features.contacts import feature as contacts_feature
from partpilot.web.features.customer_orders import feature as co_feature
from partpilot.web.features.purchase_orders import feature as po_feature
from partpilot.web.features.work_orders import feature as wo_feature

OLD = [dataclasses.replace(catalog_feature, migrations=catalog_feature.migrations[:22]),
       asm_feature, contacts_feature, co_feature, wo_feature,
       dataclasses.replace(po_feature, migrations=po_feature.migrations[:5])]
NEW = [catalog_feature, asm_feature, contacts_feature, co_feature, wo_feature, po_feature]


def _old_db(tmp_path):
    db = Database(tmp_path / "live.db")
    reg = FeatureRegistry(); reg.register(*OLD)
    db.apply_migrations(reg)
    return db


def _seed(conn):
    """Live-shaped data: 2 pairs linked by minimrp_id, 1 by name (case differs), 1 orphan supplier,
    a customer sharing an orphan's name, two same-named supplier contacts, an offer with no supplier."""
    conn.executescript("""
        INSERT INTO contacts (id, kind, name, minimrp_id, source, currency) VALUES
          (101,'supplier','Farnell',   7, 'sup','GBP'),
          (102,'supplier','RS',        8, 'sup','SEK'),
          (103,'supplier','Acme Parts', NULL,'ui', NULL),
          (104,'customer','Digi-Key',  NULL,'ui', NULL),
          (105,'supplier','Dupe Co',   NULL,'ui', NULL),
          (106,'supplier','DUPE CO',   NULL,'ui', NULL);
        INSERT INTO suppliers (id, name, short_name, url, currency, minimrp_id) VALUES
          (1,'Farnell (old name)', NULL, NULL, 'GBP', 7),
          (2,'RS',                 NULL, NULL, 'SEK', 8),
          (3,'ACME PARTS ',        NULL, NULL, NULL, NULL),
          (4,'Digi-Key',           'DK', 'https://digikey.se', 'SEK', NULL),
          (5,'dupe co',            NULL, NULL, NULL, NULL);
        INSERT INTO parts (id, part_no) VALUES (1,'P1'),(2,'P2'),(3,'P3');
        INSERT INTO part_suppliers (id, part_id, supplier_id, supplier_pno, qty_per_uom, is_default) VALUES
          (1,1,1,'F-1',1,1),(2,1,4,'DK-1',1,0),(3,2,3,'A-1',1,1),(4,3,NULL,NULL,1,1),(5,2,5,'D-1',1,0);
        INSERT INTO part_supplier_tiers (part_supplier_id, kind, break_qty, unit_price) VALUES
          (1,'cut',1,0.5),(1,'cut',100,0.4),(2,'cut',1,0.9);
        INSERT INTO purchase_orders (id, po_no, supplier_id, status) VALUES (1,'PO-1',4,'ordered'),(2,'PO-2',2,'draft');
        INSERT INTO goods_receipts (id, grn_no, po_id, supplier_id) VALUES (1,'GRN-1',1,4);
    """)
    conn.commit()


def test_migration_maps_every_supplier_to_a_contact_and_drops_the_table(tmp_path):
    db = _old_db(tmp_path)
    with db.connect() as conn:
        _seed(conn)
    reg = FeatureRegistry(); reg.register(*NEW)
    db.apply_migrations(reg)
    with db.connect() as conn:
        assert not table_exists(conn, "suppliers")
        ps = {r[0]: r[1] for r in conn.execute("SELECT id, supplier_id FROM part_suppliers")}
        dk = conn.execute("SELECT id FROM contacts WHERE kind='supplier' AND name='Digi-Key'").fetchone()[0]
        assert dk not in (101, 102, 103, 104, 105, 106)          # a NEW supplier contact, not the customer
        assert ps == {1: 101, 2: dk, 3: 103, 4: None, 5: 105}      # minimrp id / name (trimmed, case) / orphan / NULL / lowest dupe
        row = conn.execute("SELECT short_name, website, currency, source FROM contacts WHERE id=?", (dk,)).fetchone()
        assert tuple(row) == ("DK", "https://digikey.se", "SEK", "sup-merge")
        assert conn.execute("SELECT COUNT(*) FROM part_supplier_tiers").fetchone()[0] == 3
        assert {r[0]: r[1] for r in conn.execute("SELECT id, supplier_id FROM purchase_orders")} == {1: dk, 2: 102}
        assert conn.execute("SELECT supplier_id FROM goods_receipts").fetchone()[0] == dk
        for t in ("part_suppliers", "purchase_orders", "goods_receipts"):
            sql = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (t,)).fetchone()[0]
            assert "REFERENCES contacts(id)" in sql and "suppliers(id)" not in sql
            assert not conn.execute(f"PRAGMA foreign_key_check({t})").fetchall()
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert {"ix_partsup_part", "ix_po_supplier", "ix_po_status", "ix_grn_po"} <= names
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_fresh_database_ends_with_contacts_fks_and_no_suppliers_table(tmp_path):
    db = Database(tmp_path / "fresh.db")
    reg = FeatureRegistry(); reg.register(*NEW)
    db.apply_migrations(reg)
    with db.connect() as conn:
        assert not table_exists(conn, "suppliers")
        for t in ("part_suppliers", "purchase_orders", "goods_receipts"):
            assert "REFERENCES contacts(id)" in conn.execute(
                "SELECT sql FROM sqlite_master WHERE name=?", (t,)).fetchone()[0]


def test_catalog_only_registry_still_migrates(tmp_path):
    """Test registries that register catalog without contacts must keep working (no rows to map)."""
    db = Database(tmp_path / "c.db")
    reg = FeatureRegistry(); reg.register(catalog_feature)
    db.apply_migrations(reg)
    with db.connect() as conn:
        assert "REFERENCES contacts(id)" in conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='part_suppliers'").fetchone()[0]


def test_dangling_supplier_reference_fails_the_migration_and_changes_nothing(tmp_path):
    db = _old_db(tmp_path)
    with db.connect() as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.executescript("""
            INSERT INTO parts (id, part_no) VALUES (1,'P1');
            INSERT INTO part_suppliers (id, part_id, supplier_id, qty_per_uom) VALUES (1,1,77,1);
        """)
        conn.commit()
    reg = FeatureRegistry(); reg.register(*NEW)
    with pytest.raises(RuntimeError, match="dangling"):
        db.apply_migrations(reg)
    with db.connect() as conn:
        assert table_exists(conn, "suppliers")
        assert "REFERENCES suppliers(id)" in conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='part_suppliers'").fetchone()[0]
        assert not conn.execute("SELECT 1 FROM schema_migrations WHERE feature='catalog' AND version=23").fetchone()


def _expected_after(conn):
    dk = conn.execute("SELECT id FROM contacts WHERE kind='supplier' AND name='Digi-Key'").fetchone()[0]
    return dk, {1: 101, 2: dk, 3: 103, 4: None, 5: 105}


def test_retry_after_partial_failure_does_not_remap_twice(tmp_path):
    """Review finding: PO v6 rebuilt purchase_orders and goods_receipts in separate transactions. If the
    second failed, a restart re-applied the supplier->contact mapping to ids that were already contact
    ids. Each rebuild must be skipped once its table already references contacts."""
    from partpilot.web.core.db import rebuild_table
    from partpilot.web.features.catalog.supplier_merge import (FK_SWAP, SUPPLIER_ID_EXPR,
                                                               map_suppliers_to_contacts)
    db = _old_db(tmp_path)
    with db.connect() as conn:
        _seed(conn)
        # Simulate: catalog 23 done, PO 6 crashed after purchase_orders, before goods_receipts.
        map_suppliers_to_contacts(conn)
        rebuild_table(conn, "part_suppliers", sql_replacements=FK_SWAP, column_exprs={"supplier_id": SUPPLIER_ID_EXPR})
        rebuild_table(conn, "purchase_orders", sql_replacements=FK_SWAP, column_exprs={"supplier_id": SUPPLIER_ID_EXPR})
        dk, expected = _expected_after(conn)
        assert {r[0]: r[1] for r in conn.execute("SELECT id, supplier_id FROM purchase_orders")} == {1: dk, 2: 102}
    reg = FeatureRegistry(); reg.register(*NEW)
    db.apply_migrations(reg)                      # the operator's "fix and restart"
    with db.connect() as conn:
        assert {r[0]: r[1] for r in conn.execute("SELECT id, supplier_id FROM part_suppliers")} == expected
        assert {r[0]: r[1] for r in conn.execute("SELECT id, supplier_id FROM purchase_orders")} == {1: dk, 2: 102}
        assert conn.execute("SELECT supplier_id FROM goods_receipts").fetchone()[0] == dk
        assert not table_exists(conn, "suppliers")
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()


def test_minimrp_id_match_only_uses_supplier_sourced_contacts(tmp_path):
    """miniMRP AddIDs collide across its three address tables: a customer-sourced contact that was
    later edited to kind 'supplier' must not capture a supplier's rows just because the ids match."""
    db = _old_db(tmp_path)
    with db.connect() as conn:
        conn.executescript("""
            INSERT INTO contacts (id, kind, name, minimrp_id, source) VALUES
              (201,'supplier','Shadow Co', 7, 'cus'),
              (202,'supplier','Farnell',   7, 'sup');
            INSERT INTO suppliers (id, name, minimrp_id) VALUES (1,'Farnell', 7);
            INSERT INTO parts (id, part_no) VALUES (1,'P1');
            INSERT INTO part_suppliers (id, part_id, supplier_id, qty_per_uom) VALUES (1,1,1,1);
        """)
        conn.commit()
    reg = FeatureRegistry(); reg.register(*NEW)
    db.apply_migrations(reg)
    with db.connect() as conn:
        assert conn.execute("SELECT supplier_id FROM part_suppliers").fetchone()[0] == 202
