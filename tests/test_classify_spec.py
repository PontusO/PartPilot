from digisearch.config import Settings
from digisearch.models import BomLine, CompType, LineKind
from digisearch.spec.classify import classify, looks_like_mpn
from digisearch.spec.parse_spec import build_query, build_spec, extract_package

S = Settings()


def line(**kw):
    return BomLine(**kw)


def test_classify_generic_passive():
    l = line(value="0.1uF", device="C_CHIP-0402(1005-METRIC)", description="Capacitor - Generic")
    assert classify(l, S) == LineKind.GENERIC_PASSIVE


def test_classify_resistor_and_inductor():
    assert classify(line(value="4K7", device="R_CHIP-0402(1005-METRIC)"), S) == LineKind.GENERIC_PASSIVE
    assert classify(line(value="10uH", device="L_CHIP-0603(1608-METRIC)"), S) == LineKind.GENERIC_PASSIVE


def test_classify_mpn_and_numeric_ordercode():
    assert classify(line(value="USBLC6-2SC6", device="USBLC6-2SC6"), S) == LineKind.MPN
    assert classify(line(value="68711014022", device="68711014022"), S) == LineKind.MPN


def test_classify_dnp_and_non_orderable():
    assert classify(line(value="DNM", device="R_CHIP-0402(1005-METRIC)"), S) == LineKind.DNP
    assert classify(line(value="", device="TESTPOINTROUND1.5MM"), S) == LineKind.NON_ORDERABLE
    assert classify(line(value="MOUNT-HOLE2.8", device="MOUNT-HOLE2.8"), S) == LineKind.NON_ORDERABLE


def test_classify_crystal():
    assert classify(line(value="12MHz", device="XTAL-4-3225"), S) == LineKind.CRYSTAL


def test_looks_like_mpn():
    assert looks_like_mpn("ADV7513BSWZ")
    assert looks_like_mpn("532610271")  # 9-digit order code
    assert not looks_like_mpn("0R")
    assert not looks_like_mpn("100K")


def test_extract_package():
    assert extract_package(line(device="C_CHIP-0402(1005-METRIC)", package="CAPC1005X60")) == "0402"
    assert extract_package(line(device="L_CHIP-0805(2012-METRIC)")) == "0805"
    assert extract_package(line(device="C-EUC0402", package="C0402")) == "0402"


def test_build_spec_capacitor_assumptions():
    l = line(value="0.1uF", device="C_CHIP-0402(1005-METRIC)", description="Capacitor - Generic")
    spec = build_spec(l, S, LineKind.GENERIC_PASSIVE)
    assert spec.comp_type == CompType.CAPACITOR
    assert spec.value_display == "100nF"
    assert spec.package_imperial == "0402"
    assert "voltage" in spec.assumed and "dielectric" in spec.assumed
    q = build_query(spec).lower()
    assert "100nf" in q and "0402" in q and "capacitor" in q


def test_build_spec_small_cap_is_c0g():
    spec = build_spec(line(value="15pF", device="C_CHIP-0402(1005-METRIC)"), S, LineKind.GENERIC_PASSIVE)
    assert spec.dielectric == "C0G"


def test_build_spec_parses_rkm_cap_and_inductor_notation():
    # House style writes 0.1uF as "0u1"; the BOM side must understand it like stock does.
    for value in ("0u1", "0u1F"):
        spec = build_spec(line(value=value, device="C_CHIP-0402(1005-METRIC)"), S, LineKind.GENERIC_PASSIVE)
        assert spec.value_si == 1e-7, value
        assert spec.value_display == "100nF"
    ind = build_spec(line(value="3n3", device="L_CHIP-0402(1005-METRIC)"), S, LineKind.GENERIC_PASSIVE)
    assert ind.comp_type == CompType.INDUCTOR and abs(ind.value_si - 3.3e-9) < 1e-15


def test_resistor_query_is_ascii_no_ohm_symbol():
    from digisearch.spec.parse_spec import resistance_query_token

    assert resistance_query_token(0) == "0 ohm"
    assert resistance_query_token(100) == "100R"
    assert resistance_query_token(4.7) == "4R7"
    assert resistance_query_token(4700) == "4.7k"
    assert resistance_query_token(100000) == "100k"
    for value in ("100K", "4K7", "0R", "887"):
        q = build_query(build_spec(line(value=value, device="R_CHIP-0402(1005-METRIC)"), S, LineKind.GENERIC_PASSIVE))
        assert "Ω" not in q and "resistor" in q


def test_capacitor_query_uses_ascii_micro():
    q = build_query(build_spec(line(value="2.2uF", device="C_CHIP-0402(1005-METRIC)"), S, LineKind.GENERIC_PASSIVE))
    assert "µ" not in q and "2.2uF" in q


def test_relaxed_mpn_queries():
    from digisearch.spec.parse_spec import relaxed_mpn_queries

    assert relaxed_mpn_queries("SL3401A-TP") == ["SL3401A", "SL3401"]
    assert relaxed_mpn_queries("ADV7513BSWZ") == ["ADV7513"]
    assert relaxed_mpn_queries("USBLC6") == []  # nothing sensible to trim
