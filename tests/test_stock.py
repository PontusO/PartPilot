from digisearch.config import Settings
from digisearch.stock import StockIndex, StockItem, parse_value
from digisearch.models import BomLine, CompType, Status
from digisearch.pipeline import resolve_line

from conftest import CAP_PARAMS, FakeSearcher, make_product
from digisearch.digikey.client import product_to_candidate

S = Settings()


def _item(master, name, cat, comp, value, pkg, on_hand, alloc=0.0):
    return StockItem(
        item_id=1, master_pno=master, mfr_pno="", name=name, description="",
        category=cat, comp_type=comp, value_si=value, package=pkg,
        on_hand=on_hand, allocated=alloc, on_order=0.0,
    )


def _index():
    items = [
        _item("GRM-0805", "0u1/16V/10%/0402", "CAPACITOR", CompType.CAPACITOR, 1e-7, "0402", 20000),
        _item("RES-100K", "100K/1%/0.0625W/0402", "RESISTOR", CompType.RESISTOR, 1e5, "0402", 12000, alloc=2000),
        _item("MT3410LB", "", "INTEGRATED CIRCUIT", CompType.OTHER, None, None, 4000),
    ]
    return StockIndex.build(items)


def test_parse_name_value_and_package():
    assert parse_value("0u1/16V/10%/0402", CompType.CAPACITOR) == (1e-7, "0402")
    assert parse_value("100K/1%/0.0625W/0402", CompType.RESISTOR)[1] == "0402"


def test_match_mpn_and_param():
    idx = _index()
    assert idx.match_mpn("MT3410LB").free == 4000
    assert idx.match_mpn("nope") is None
    cap = idx.match_param(CompType.CAPACITOR, 1e-7, "0402")
    assert cap is not None and cap.free == 20000
    assert idx.match_param(CompType.CAPACITOR, 1e-7, "0805") is None  # wrong package


def test_free_is_on_hand_minus_allocated():
    idx = _index()
    assert idx.match_mpn("RES-100K").free == 10000  # 12000 - 2000 allocated


def _cap_line():
    return BomLine(refdes=["C4", "C7", "C8"], qty=3, value="0.1uF",
                   device="C_CHIP-0402(1005-METRIC)", description="Capacitor - Generic")


def test_fully_stocked_passive_skips_digikey():
    client = FakeSearcher(default=[product_to_candidate(make_product(params=CAP_PARAMS))])
    res = resolve_line(_cap_line(), client, S, build_qty=100, stock=_index())
    assert res.status == Status.IN_STOCK
    assert res.need_to_buy == 0
    assert res.chosen is None
    assert client.calls == []  # Digi-Key never queried -> quota saved


def test_partial_stock_buys_shortfall_and_queries_digikey():
    # need 300 (3 x 100), only 100 free -> buy 200
    idx = StockIndex.build([
        _item("GRM-0805", "0u1/16V/10%/0402", "CAPACITOR", CompType.CAPACITOR, 1e-7, "0402", 100),
    ])
    client = FakeSearcher(default=[product_to_candidate(make_product(params=CAP_PARAMS))])
    res = resolve_line(_cap_line(), client, S, build_qty=100, stock=idx)
    assert res.need_to_buy == 200
    assert res.stock_free == 100
    assert res.chosen is not None and client.calls  # still priced via Digi-Key


def test_mpn_in_stock_recovers_digikey_miss():
    # MT3410LB isn't on Digi-Key but is in stock -> IN_STOCK without a found part
    client = FakeSearcher(default=[])  # DK returns nothing
    line = BomLine(refdes=["U15"], qty=1, value="MT3410LB", device="MT3410LB")
    res = resolve_line(line, client, S, build_qty=100, stock=_index())
    assert res.status == Status.IN_STOCK
    assert res.stock_match == "MT3410LB"


def _mpn_item(master, on_hand):
    return _item(master, master, "SEMICONDUCTOR", CompType.OTHER, None, None, on_hand)


def test_mpn_stem_matches_fuller_stocked_part():
    idx = StockIndex.build([_mpn_item("MBR120LSF", 5000)])
    assert idx.match_mpn("MBR120") is None          # exact fails
    hit = idx.match_mpn_prefix("MBR120")            # stem matches
    assert hit is not None and hit.master_pno == "MBR120LSF"


def test_mpn_stem_does_not_merge_into_longer_number():
    idx = StockIndex.build([_mpn_item("MBR1200", 5000)])  # 200V part, different value
    assert idx.match_mpn_prefix("MBR120") is None


def test_mpn_stem_matches_across_hyphen_before_digits():
    # The separator is a valid boundary even though digits follow it.
    idx = StockIndex.build([_mpn_item("XC6565-12", 5000)])
    assert idx.match_mpn("XC6565") is None
    assert idx.match_mpn_prefix("XC6565").master_pno == "XC6565-12"


def test_mpn_stem_matches_hyphen_letter_suffix():
    idx = StockIndex.build([_mpn_item("FG887-LO12", 5000)])
    assert idx.match_mpn_prefix("FG887").master_pno == "FG887-LO12"


def test_mpn_stem_rejects_digit_run_without_separator():
    # No separator and the number just continues -> still rejected.
    idx = StockIndex.build([_mpn_item("XC65651", 5000)])
    assert idx.match_mpn_prefix("XC6565") is None


# --- crystals: generic frequency -> stocked MPN ---

def test_parse_name_crystal_frequency_and_package():
    v, p = parse_value(
        "12MHz Crystal Oscillator 12pF ±10ppm ±20ppm SMD3225-4P Crystals ROHS", CompType.CRYSTAL
    )
    assert v == 12e6 and p == "3225"
    # frequency from the description when ItemName is empty
    assert parse_value("", CompType.CRYSTAL, "CRYSTAL 32.7680KHZ 9PF SMD")[0] == 32768.0
    # outline from spelled-out dimensions
    v3, p3 = parse_value("Crystal, 26 MHz, SMD, 3.2mm x 2.5mm, 10 pF", CompType.CRYSTAL)
    assert v3 == 26e6 and p3 == "3225"


def _xtal_item(master, value_si, pkg, on_hand):
    return _item(master, master, "CRYSTAL", CompType.CRYSTAL, value_si, pkg, on_hand)


def test_match_crystal_by_frequency_and_package():
    idx = StockIndex.build([
        _xtal_item("X322512MOB4SI", 12e6, "3225", 5000),
        _xtal_item("RH100-32.000-10", 32e6, "3225", 8000),
    ])
    assert idx.match_crystal(12e6, "3225").master_pno == "X322512MOB4SI"
    assert idx.match_crystal(12e6, None).master_pno == "X322512MOB4SI"  # freq-only still matches
    assert idx.match_crystal(48e6, "3225") is None                      # no 48 MHz in stock


def test_generic_crystal_matches_stocked_mpn_in_pipeline():
    idx = StockIndex.build([_xtal_item("X322512MOB4SI", 12e6, "3225", 5000)])
    client = FakeSearcher(default=[])
    line = BomLine(refdes=["Q2"], qty=1, value="12MHz", device="XTAL-4-3225",
                   package="NX3225", description="Classic 4-pin 3.2 x 2.5mm crystal")
    res = resolve_line(line, client, S, build_qty=100, stock=idx)
    assert res.status == Status.IN_STOCK
    assert res.stock_match == "X322512MOB4SI"
    assert "crystal" in (res.flag_reason or "").lower()  # flagged to verify
    assert client.calls == []                            # skipped Digi-Key


def test_passive_stock_match_has_no_verify_note():
    # Parametric passive matches are trusted -> must NOT carry a stem/verify note.
    client = FakeSearcher(default=[product_to_candidate(make_product(params=CAP_PARAMS))])
    res = resolve_line(_cap_line(), client, S, build_qty=100, stock=_index())
    assert res.status == Status.IN_STOCK
    assert "verify" not in (res.flag_reason or "").lower()
    assert "stem" not in (res.flag_reason or "").lower()


def test_mpn_stem_prefers_more_free_when_ambiguous():
    idx = StockIndex.build([_mpn_item("MBR120LSF", 100), _mpn_item("MBR120T3G", 9000)])
    assert idx.match_mpn_prefix("MBR120").master_pno == "MBR120T3G"


def test_mpn_stem_respects_min_len():
    assert StockIndex.build([_mpn_item("ABCD", 5000)]).match_mpn_prefix("AB") is None


def test_matches_mpn_flag():
    it = _mpn_item("MBR120LSF", 5000)
    assert it.matches_mpn("MBR120LSF") is True
    assert it.matches_mpn("mbr120-lsf") is True       # normalized
    assert it.matches_mpn("MBR120") is False


def test_stem_stock_match_in_pipeline_flags_for_verify():
    idx = StockIndex.build([_mpn_item("MBR120LSF", 5000)])
    client = FakeSearcher(default=[])
    line = BomLine(refdes=["D1"], qty=1, value="MBR120", device="MBR120")
    res = resolve_line(line, client, S, build_qty=100, stock=idx)
    assert res.status == Status.IN_STOCK
    assert res.stock_match == "MBR120LSF"
    assert "stem" in (res.flag_reason or "").lower()
    assert client.calls == []  # still skips Digi-Key
