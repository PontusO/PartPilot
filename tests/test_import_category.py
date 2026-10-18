"""The BOM importer guesses a catalog category: resolver component type, then the distributor's
category, then the RefDes prefix. Names come from config/category_rules.yaml and must match the
catalog's own vocabulary."""

import pytest

from partpilot.digikey.client import product_to_candidate
from partpilot.mouser.client import part_to_candidate
from conftest import make_mouser_part, make_product


def test_digikey_candidate_carries_the_category_path():
    p = make_product()
    p["Category"] = {"CategoryId": 3, "Name": "Capacitors", "ChildCategories": [
        {"CategoryId": 60, "Name": "Ceramic Capacitors", "ChildCategories": []}]}
    assert product_to_candidate(p).category == "Capacitors > Ceramic Capacitors"


def test_digikey_candidate_without_category_is_fine():
    assert product_to_candidate(make_product()).category is None


def test_mouser_candidate_carries_the_category():
    p = make_mouser_part()
    p["Category"] = "Microcontrollers - MCU"
    assert part_to_candidate(p).category == "Microcontrollers - MCU"
