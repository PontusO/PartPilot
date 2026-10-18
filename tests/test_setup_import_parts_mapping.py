import pytest

from partpilot.web.core import FeatureRegistry
from partpilot.web.core.db import Database
from partpilot.web.features.setup import feature as setup_feature
from partpilot.web.features.setup import import_parts as ip


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "s.db")
    reg = FeatureRegistry()
    reg.register(setup_feature)
    database.apply_migrations(reg)
    return database


def test_read_headers_skips_title_rows(tmp_path):
    p = tmp_path / "bom.csv"
    p.write_text("My Project BOM,,,\nexported 2026-10-15,,,\n"
                 "Designator,Value,Footprint,Qty\nR1,10k,0402,1\nC1,100n,0402,1\nU1,MCU,QFN,1\nX1,,,1\n")
    headers, samples = ip.read_headers(p)
    assert headers == ["Designator", "Value", "Footprint", "Qty"]
    assert samples[1] == ["10k", "100n", "MCU"]         # max 3, non-empty only
    assert samples[0] == ["R1", "C1", "U1"]


def test_read_headers_rejects_unknown_suffix(tmp_path):
    p = tmp_path / "bom.pdf"
    p.write_bytes(b"%PDF")
    with pytest.raises(ValueError):
        ip.read_headers(p)


def test_header_key_normalises_but_is_order_sensitive():
    a = ip.header_key(["Designator", "Value", "Qty"])
    assert a == ip.header_key([" designator", "VALUE ", "qty"])
    assert a != ip.header_key(["Value", "Designator", "Qty"])
    assert a != ip.header_key(["Designator", "Value", "Qty", "Extra"])
    assert a.startswith("bom_import.mapping.")


def test_guess_mapping_uses_aliases():
    m = ip.guess_mapping(["Designator", "Value", "Footprint", "Qty", "Weird"])
    assert m == {"refdes": "Designator", "value": "Value", "package": "Footprint", "qty": "Qty"}


def test_prefill_prefers_remembered(db):
    headers = ["Designator", "Wert", "Value"]
    assert ip.remembered_mapping(db, headers) is None
    assert ip.prefill_mapping(db, headers)["value"] == "Value"      # alias guess
    ip.remember_mapping(db, headers, {"refdes": "Designator", "value": "Wert"})
    assert ip.prefill_mapping(db, headers) == {"refdes": "Designator", "value": "Wert"}
    assert ip.remembered_mapping(db, ["Wert", "Designator", "Value"]) is None   # reordered


def test_parse_mapping_ok():
    headers = ["Designator", "Value", "Notes"]
    m = ip.parse_mapping({"map_0": "refdes", "map_1": "value", "map_2": "ignore"}, headers)
    assert m == {"refdes": "Designator", "value": "Value"}
    assert ip.run_mappings(m) == {"refdes": ["Designator"], "value": ["Value"]}


def test_parse_mapping_rejects_duplicate_target():
    with pytest.raises(ValueError, match="more than once"):
        ip.parse_mapping({"map_0": "value", "map_1": "value"}, ["A", "B"])


def test_parse_mapping_requires_value_or_device():
    with pytest.raises(ValueError, match="Value or Device"):
        ip.parse_mapping({"map_0": "refdes", "map_1": "qty"}, ["A", "B"])


def test_parse_mapping_ignores_unknown_targets_and_indexes():
    m = ip.parse_mapping({"map_0": "device", "map_1": "bogus", "map_9": "value", "x": "y"}, ["A", "B"])
    assert m == {"device": "A"}


def test_read_headers_dedupes_duplicate_names(tmp_path):
    p = tmp_path / "dup.csv"
    p.write_text("Qty,Value,Description,Description\n1,10k,res,extra\n")
    headers, samples = ip.read_headers(p)
    assert headers == ["Qty", "Value", "Description", "Description (2)"]
    assert samples[3] == ["extra"]


def test_manufacturer_is_a_mapping_target():
    assert dict(ip.TARGETS)["manufacturer"] == "Manufacturer"
    m = ip.parse_mapping({"map_0": "value", "map_1": "manufacturer"}, ["Value", "Mfr"])
    assert m == {"value": "Value", "manufacturer": "Mfr"}


def test_guess_mapping_prefills_manufacturer():
    m = ip.guess_mapping(["Designator", "Value", "Manufacturer", "Manufacturer Part Number"])
    assert m["manufacturer"] == "Manufacturer"
