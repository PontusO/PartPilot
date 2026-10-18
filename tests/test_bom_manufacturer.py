"""A BOM's manufacturer column reaches the line model and, from there, the created part."""

import pandas as pd

from partpilot.bom.normalize import detect_columns, normalize_frame
from partpilot.config import load_column_mappings


def test_normalize_frame_reads_mapped_manufacturer_column():
    df = pd.DataFrame({"Designator": ["R1"], "Value": ["10k"], "Maker": ["Yageo"]})
    lines = normalize_frame(df, {"refdes": ["Designator"], "value": ["Value"],
                                 "manufacturer": ["Maker"]})
    assert lines[0].manufacturer == "Yageo"


def test_manufacturer_is_blank_when_not_mapped():
    df = pd.DataFrame({"Designator": ["R1"], "Value": ["10k"], "Maker": ["Yageo"]})
    lines = normalize_frame(df, {"refdes": ["Designator"], "value": ["Value"]})
    assert lines[0].manufacturer is None


def test_default_aliases_detect_common_manufacturer_headers():
    aliases = load_column_mappings()
    for header in ("Manufacturer", "Mfr", "MFG", "Tillverkare"):
        got = detect_columns(["Designator", "Value", header], aliases)
        assert got.get("manufacturer") == header, header


def test_manufacturer_alias_does_not_steal_the_mpn_column():
    aliases = load_column_mappings()
    got = detect_columns(["Designator", "Value", "Manufacturer Part Number"], aliases)
    assert "manufacturer" not in got


def test_compound_headers_are_not_fuzzy_matched_as_manufacturer():
    """'Manufacturer PN' is a different column, not a typo of 'Manufacturer'."""
    aliases = load_column_mappings()
    for header in ("Manufacturer PN", "Manufacturer No", "Mfr Part #", "Manufacturer Part"):
        got = detect_columns(["Designator", "Value", header], aliases)
        assert "manufacturer" not in got, header


def test_typo_in_manufacturer_header_still_matches():
    got = detect_columns(["Designator", "Value", "Manufacurer"], load_column_mappings())
    assert got.get("manufacturer") == "Manufacurer"
