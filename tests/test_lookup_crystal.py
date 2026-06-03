from digisearch.config import Settings
from digisearch.models import BomLine, CompType, LineKind, Status
from digisearch.pipeline import plan_line, resolve_line
from digisearch.spec.lookup import LookupRule, load_lookup, match_lookup, render
from digisearch.spec.parse_spec import build_spec, format_dimension_code

from conftest import FakeSearcher, make_product
from digisearch.digikey.client import product_to_candidate

S = Settings()


def test_default_lookup_file_loads():
    rules = load_lookup(None)  # ships with the repo
    assert rules and any(r.query for r in rules)


def test_led_lookup_builds_color_query():
    rules = load_lookup(None)
    line = BomLine(refdes=["LED2"], qty=1, value="GREEN", device="LEDCHIPLED_0603")
    plan = plan_line(line, S, rules)
    assert plan.kind == LineKind.LOOKUP
    assert "green" in plan.query.lower() and "0603" in plan.query


def test_lookup_mpn_rule_routes_to_confident_mpn():
    rules = [LookupRule(device_re=None, value_re=None, mpn=None, query=None)]
    # build a rule by hand: exact MPN for the KMR2 switch
    import re

    rules = [LookupRule(device_re=re.compile("KMR2", re.I), value_re=None,
                        mpn="KMR221GLFS", query=None)]
    line = BomLine(refdes=["SW1"], qty=1, value="SPST_TACT-KMR2", device="SPST_TACT-KMR2")
    plan = plan_line(line, S, rules)
    assert plan.kind == LineKind.MPN and plan.target_mpn == "KMR221GLFS"


def test_lookup_resolution_is_flagged_review():
    rules = load_lookup(None)
    cand = product_to_candidate(make_product(mpn="150060GS75000", manufacturer="Würth", qty=9000))
    client = FakeSearcher(default=[cand])
    line = BomLine(refdes=["LED2"], qty=1, value="GREEN", device="LEDCHIPLED_0603")
    res = resolve_line(line, client, S, lookup=rules)
    assert res.status == Status.REVIEW
    assert res.chosen.mpn == "150060GS75000"


def test_render_substitutes_placeholders():
    line = BomLine(value="RED", device="LEDCHIPLED_0805")
    assert render("{value} LED {package} SMD", line) == "RED LED 0805 SMD"


def test_crystal_uses_outline_dimensions_not_eia():
    line = BomLine(refdes=["Q2"], qty=1, value="12MHz", device="XTAL-4-3225", package="NX3225")
    spec = build_spec(line, S, LineKind.CRYSTAL)
    assert spec.comp_type == CompType.CRYSTAL
    assert spec.package_imperial is None          # not coerced to 1210
    assert spec.package_note == "3.2 x 2.5 mm"


def test_format_dimension_code():
    assert format_dimension_code("3225") == "3.2 x 2.5 mm"
    assert format_dimension_code("2016") == "2.0 x 1.6 mm"
