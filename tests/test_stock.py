from digisearch.config import Settings
from digisearch.minimrp.reader import StockIndex, StockItem, _parse_name
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
    assert _parse_name("0u1/16V/10%/0402", CompType.CAPACITOR) == (1e-7, "0402")
    assert _parse_name("100K/1%/0.0625W/0402", CompType.RESISTOR)[1] == "0402"


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
