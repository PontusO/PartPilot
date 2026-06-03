import pytest

from digisearch.config import Settings
from digisearch.models import BomLine, Status
from digisearch.mouser.client import parse_availability, parse_price, part_to_candidate
from digisearch.pipeline import resolve_line

from conftest import FakeSearcher, make_mouser_part, make_product
from digisearch.digikey.client import product_to_candidate

S = Settings()


@pytest.mark.parametrize(
    "text,expected",
    [("$0.10", 0.10), ("0,10 €", 0.10), ("1.234,56 kr", 1234.56),
     ("1,234.56", 1234.56), ("SEK 2,50", 2.50), ("", None)],
)
def test_parse_price(text, expected):
    assert parse_price(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [("5000 In Stock", 5000), ("1,234 In Stock", 1234), ("Quote", 0), ("", 0), (None, 0)],
)
def test_parse_availability(text, expected):
    assert parse_availability(text) == expected


def test_part_to_candidate():
    c = part_to_candidate(make_mouser_part())
    assert c.supplier == "Mouser"
    assert c.mpn == "APS6404L-3SQR"
    assert c.dk_part_number == "81-APS6404L-3SQR"
    assert c.quantity_available == 5000
    assert c.price_breaks == [(1, 1.50), (100, 1.20)]
    assert c.price_at(150) == 1.20


def _mpn_line(value="APS6404L-3"):
    return BomLine(refdes=["U6"], qty=1, value=value, device=value)


def test_mouser_used_when_digikey_weak():
    # Digi-Key returns a poor match (wrong MPN); Mouser has the real part.
    dk = FakeSearcher(default=[product_to_candidate(make_product(mpn="MIKROE-5337"))])
    mo = FakeSearcher(default=[part_to_candidate(make_mouser_part(mpn="APS6404L-3"))])
    res = resolve_line(_mpn_line(), dk, S, mouser=mo)
    assert res.chosen.supplier == "Mouser"
    assert res.chosen.mpn == "APS6404L-3"
    assert "sourced from Mouser" in (res.flag_reason or "")


def test_digikey_preferred_when_confident():
    # Exact Digi-Key match -> Mouser must NOT be consulted.
    dk = FakeSearcher(default=[product_to_candidate(make_product(mpn="APS6404L-3"))])
    mo = FakeSearcher(default=[part_to_candidate(make_mouser_part(mpn="APS6404L-3"))])
    res = resolve_line(_mpn_line(), dk, S, mouser=mo)
    assert res.chosen.supplier == "Digi-Key"
    assert res.status == Status.RESOLVED
    assert mo.calls == []  # Digi-Key was confident, Mouser skipped


def test_mouser_recovers_digikey_not_found():
    dk = FakeSearcher(default=[])  # Digi-Key finds nothing
    mo = FakeSearcher(default=[part_to_candidate(make_mouser_part(mpn="APS6404L-3"))])
    res = resolve_line(_mpn_line(), dk, S, mouser=mo)
    assert res.chosen.supplier == "Mouser"
    assert res.status in (Status.RESOLVED, Status.REVIEW)


def test_digikey_kept_on_tie():
    # Both have the exact part -> Digi-Key wins (preferred).
    dk = FakeSearcher(default=[product_to_candidate(make_product(mpn="APS6404L-3"))])
    mo = FakeSearcher(default=[part_to_candidate(make_mouser_part(mpn="APS6404L-3"))])
    res = resolve_line(_mpn_line(), dk, S, mouser=mo)
    assert res.chosen.supplier == "Digi-Key"
