from pathlib import Path

from digisearch.config import Settings
from digisearch.models import BomLine, LineKind, Status
from digisearch.pipeline import resolve_bom, resolve_line
from digisearch.report.excel import write_report

from conftest import CAP_PARAMS, FakeSearcher, make_product
from digisearch.digikey.client import product_to_candidate

S = Settings()


def cap_line():
    return BomLine(
        refdes=["C4", "C7", "C8"], qty=3, value="0.1uF",
        device="C_CHIP-0402(1005-METRIC)", description="Capacitor - Generic",
    )


def test_product_to_candidate_extracts_pricing_and_params():
    cand = product_to_candidate(make_product(params=CAP_PARAMS))
    assert cand.mpn == "CL05B104KO5NNNC"
    assert cand.dk_part_number == "1276-1000-1-ND"
    assert cand.quantity_available == 50000
    assert cand.price_breaks[0] == (1, 0.10)
    assert cand.price_at(150) == 0.02  # 100-break price for qty 150
    assert cand.parameters["Capacitance"] == "0.1 µF"


def test_resolve_passive_matches_and_prices():
    cand = product_to_candidate(make_product(params=CAP_PARAMS))
    client = FakeSearcher(default=[cand])
    res = resolve_line(cap_line(), client, S, build_qty=100)
    assert res.kind == LineKind.GENERIC_PASSIVE
    assert res.chosen.mpn == "CL05B104KO5NNNC"
    # under-specified passives are always surfaced for review
    assert res.status == Status.REVIEW
    assert res.confidence > 0.7
    # 3 per board * 100 boards = 300 -> 100-break price
    assert res.build_unit_price(300) == 0.02


def test_resolve_mpn_exact_match_is_confident():
    prod = make_product(mpn="USBLC6-2SC6", manufacturer="ST", dkpn="497-1-ND")
    client = FakeSearcher(default=[product_to_candidate(prod)])
    bl = BomLine(refdes=["U3"], qty=1, value="USBLC6-2SC6", device="USBLC6-2SC6")
    res = resolve_line(bl, client, S)
    assert res.kind == LineKind.MPN
    assert res.status == Status.RESOLVED
    assert res.confidence > 0.9


def test_valueless_lines_need_manual():
    client = FakeSearcher(default=[product_to_candidate(make_product())])
    # an inductor with no value -> nothing to parametric-match on
    l1 = resolve_line(BomLine(refdes=["L1"], qty=1, value="",
                              device="L_CHIP-0805(2012-METRIC)", package="INDC2009X120",
                              description="Inductor Fixed - Generic"), client, S)
    assert l1.status == Status.MANUAL
    # SV1: empty value, device == package (a generic EAGLE part name, not a real MPN)
    sv1 = resolve_line(BomLine(refdes=["SV1"], qty=1, value="",
                               device="MA13-2", package="MA13-2", description="PIN HEADER"), client, S)
    assert sv1.status == Status.MANUAL
    assert client.calls == []  # neither one was searched on Digi-Key


def test_resolve_not_found_and_dnp():
    client = FakeSearcher(default=[])
    nf = resolve_line(BomLine(refdes=["U1"], qty=1, value="NOSUCHPART123"), client, S)
    assert nf.status == Status.NOT_FOUND
    dnp = resolve_line(
        BomLine(refdes=["R1"], qty=1, value="DNM", device="R_CHIP-0402(1005-METRIC)"), client, S
    )
    assert dnp.status == Status.DNP
    assert client.calls  # MPN line searched, DNP did not add a call beyond it


def test_write_report_smoke(tmp_path: Path):
    cand = product_to_candidate(make_product(params=CAP_PARAMS))
    client = FakeSearcher(default=[cand])
    lines = [cap_line(), BomLine(refdes=["TP1"], qty=1, device="TESTPOINTROUND1.5MM")]
    resolved = resolve_bom(lines, client, S, build_qty=10)
    out = write_report(resolved, tmp_path / "out.xlsx", build_qty=10, currency="SEK")
    assert out.exists() and out.stat().st_size > 0
