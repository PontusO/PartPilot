from digisearch.config import Settings
from digisearch.models import BomLine, Candidate, Status
from digisearch.pipeline import resolve_line
from digisearch.purchasing import decide_packaging

from conftest import CAP_PARAMS, FakeSearcher, make_product
from digisearch.digikey.client import product_to_candidate

S = Settings()


def _reel_candidate():
    # cut tape: 0.09 @100; full reel of 10000 @ 0.028
    return Candidate(
        supplier="Digi-Key", mpn="RC0402", quantity_available=500000,
        price_breaks=[(1, 0.91), (100, 0.09), (250, 0.067)],
        reel_qty=10000, reel_price_breaks=[(10000, 0.028), (20000, 0.025)],
    )


def test_cheap_reel_is_bought_whole():
    d = decide_packaging(_reel_candidate(), order_qty=200, reel_threshold=10000)
    assert d.packaging == "Full reel"
    assert d.qty == 10000
    assert d.line_cost == 10000 * 0.028  # 280 < 10000


def test_expensive_reel_falls_back_to_cut_tape():
    # reel of an expensive part: 2500 x 8 = 20000 > threshold
    c = Candidate(
        supplier="Digi-Key", mpn="IC1", quantity_available=9000,
        price_breaks=[(1, 9.0), (100, 8.5)],
        reel_qty=2500, reel_price_breaks=[(2500, 8.0)],
    )
    d = decide_packaging(c, order_qty=200, reel_threshold=10000)
    assert d.packaging == "Cut tape"
    assert d.qty == 200
    assert round(d.line_cost, 2) == round(200 * 8.5, 2)


def test_multi_reel_rounds_up_to_whole_reels():
    d = decide_packaging(_reel_candidate(), order_qty=25000, reel_threshold=1e9)
    assert d.qty == 30000  # ceil(25000/10000) * 10000


def test_threshold_zero_disables_reels():
    d = decide_packaging(_reel_candidate(), order_qty=200, reel_threshold=0)
    assert d.packaging == "Cut tape"


def test_no_reel_data_is_cut_tape():
    c = Candidate(supplier="Mouser", mpn="X", quantity_available=100, price_breaks=[(1, 2.0)])
    d = decide_packaging(c, order_qty=50, reel_threshold=10000)
    assert d.packaging == "Cut tape" and d.qty == 50


def test_digikey_parses_reel_variation():
    prod = make_product(params=CAP_PARAMS)
    prod["ProductVariations"].append({
        "DigiKeyProductNumber": "TR-1", "MinimumOrderQuantity": 10000,
        "PackageType": {"Name": "Tape & Reel (TR)"},
        "StandardPricing": [{"BreakQuantity": 10000, "UnitPrice": 0.02}],
    })
    c = product_to_candidate(prod)
    assert c.reel_qty == 10000
    assert c.reel_price_at(10000) == 0.02


def test_pipeline_sets_purchase_fields():
    line = BomLine(refdes=["C1"], qty=2, value="0.1uF",
                   device="C_CHIP-0402(1005-METRIC)", description="Capacitor - Generic")
    client = FakeSearcher(default=[_reel_candidate()])
    res = resolve_line(line, client, S, build_qty=100, reel_threshold=10000)
    assert res.packaging == "Full reel"
    assert res.purchase_qty == 10000
    assert res.line_cost is not None
