from digisearch.web.core import Feature, FeatureRegistry, Migration
from digisearch.web.core.db import Database


def _registry_with_table():
    reg = FeatureRegistry()
    reg.register(
        Feature(
            name="catalog",
            migrations=[
                Migration(1, "create parts", "CREATE TABLE parts (id INTEGER PRIMARY KEY, mpn TEXT);"),
                Migration(2, "add stock col", "ALTER TABLE parts ADD COLUMN stock INTEGER DEFAULT 0;"),
            ],
        )
    )
    return reg


def test_migrations_apply_and_are_idempotent(tmp_path):
    db = Database(tmp_path / "p.db")
    reg = _registry_with_table()

    applied = db.apply_migrations(reg)
    assert applied == [("catalog", 1), ("catalog", 2)]

    # Table and both columns exist.
    with db.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(parts)")}
    assert {"id", "mpn", "stock"} <= cols

    # Running again applies nothing.
    assert db.apply_migrations(reg) == []


def test_only_pending_migrations_run(tmp_path):
    db = Database(tmp_path / "p.db")
    reg = _registry_with_table()
    db.apply_migrations(reg)

    # A new version on the same feature applies only that step.
    reg.features[0].migrations.append(
        Migration(3, "add index", "CREATE INDEX ix_parts_mpn ON parts(mpn);")
    )
    assert db.apply_migrations(reg) == [("catalog", 3)]


def test_duplicate_feature_rejected():
    reg = FeatureRegistry()
    reg.register(Feature(name="x"))
    try:
        reg.register(Feature(name="x"))
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_nav_filtered_by_role():
    from digisearch.web.core import NavItem

    reg = FeatureRegistry()
    reg.register(
        Feature(name="a", nav=NavItem("Purchasing", "/purchasing", roles=frozenset({"purchasing"}), order=10)),
        Feature(name="b", nav=NavItem("Stock", "/stock", roles=frozenset({"warehouse"}), order=20)),
        Feature(name="c", nav=NavItem("Home", "/home", roles=None, order=5)),
    )
    buyer = [n.url for n in reg.nav_for("purchasing")]
    assert buyer == ["/home", "/purchasing"]  # order respected, warehouse-only hidden
    assert [n.url for n in reg.nav_for(None)] == ["/home"]
