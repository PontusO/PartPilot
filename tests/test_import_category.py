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


# ---- rules file + guess --------------------------------------------------------------------

from partpilot.config import load_category_rules  # noqa: E402
from partpilot.models import BomLine, Candidate, CompType, LineKind, PartSpec, ResolvedLine, Status  # noqa: E402
from partpilot.web.features.catalog.import_category import guess_category  # noqa: E402


def _line(refdes="U1", comp_type=None, category=None, found=True):
    spec = PartSpec(comp_type=comp_type) if comp_type else None
    chosen = Candidate(supplier="Digi-Key", mpn="X", category=category) if found else None
    return ResolvedLine(line=BomLine(refdes=[refdes], qty=1, value="x"), kind=LineKind.MPN,
                        spec=spec, chosen=chosen,
                        status=Status.RESOLVED if found else Status.NOT_FOUND)


def test_shipped_rules_file_loads_both_sections():
    rules = load_category_rules()
    assert rules["refdes"]["R"] == "RESISTOR"
    assert any(cat == "CONNECTOR" for _, cat in rules["distributor"])


def test_resolver_component_type_wins():
    assert guess_category(_line("U1", comp_type=CompType.RESISTOR,
                                category="Integrated Circuits (ICs)")) == "RESISTOR"


@pytest.mark.parametrize("path, expected", [
    ("Integrated Circuits (ICs) > Embedded > Microcontrollers", "INTEGRATED CIRCUIT"),
    ("Microcontrollers - MCU", "INTEGRATED CIRCUIT"),                    # Mouser leaf only
    ("Connectors, Interconnects > Rectangular Connectors - Headers", "CONNECTOR"),
    ("Circuit Protection > TVS - Diodes", "SEMICONDUCTOR"),
    ("Discrete Semiconductor Products > Transistors - FETs, MOSFETs", "SEMICONDUCTOR"),
    ("Optoelectronics > LED Indication - Discrete", "SEMICONDUCTOR"),
    ("RF/IF and RFID > RF Transceiver Modules", "MODULE"),
    ("Capacitors > Ceramic Capacitors", "CAPACITOR"),                    # 'ic' inside 'Ceramic' is not IC
    ("Switches > Tactile Switches", "SWITCH"),
    ("Circuit Protection > Fuses", "FUSE"),
    ("Crystals, Oscillators, Resonators > Crystals", "CRYSTAL"),
    ("Inductors, Coils, Chokes > Fixed Inductors", "INDUCTOR"),
    ("Filters > Ferrite Beads and Chips", "INDUCTOR"),
    ("RF/IF and RFID > RF Antennas", "ANTENNA"),
    ("Battery Products > Batteries Non-Rechargeable (Primary)", "BATTERY"),
])
def test_distributor_category_maps_to_catalog_vocabulary(path, expected):
    assert guess_category(_line("Q1", category=path)) == expected


def test_distributor_category_beats_refdes_prefix():
    # EAGLE uses Q for crystals; the distributor knows better than the prefix.
    assert guess_category(_line("Q1", category="Crystals, Oscillators, Resonators > Crystals")) == "CRYSTAL"


@pytest.mark.parametrize("refdes, expected", [
    ("U1", "INTEGRATED CIRCUIT"), ("U$3", "INTEGRATED CIRCUIT"), ("IC2", "INTEGRATED CIRCUIT"),
    ("J3", "CONNECTOR"), ("X1", "CONNECTOR"), ("P2", "CONNECTOR"), ("CN1", "CONNECTOR"),
    ("D2", "SEMICONDUCTOR"), ("Q4", "SEMICONDUCTOR"), ("LED1", "SEMICONDUCTOR"), ("T1", "SEMICONDUCTOR"),
    ("R12", "RESISTOR"), ("C3", "CAPACITOR"), ("L1", "INDUCTOR"), ("FB1", "INDUCTOR"),
    ("Y1", "CRYSTAL"), ("SW1", "SWITCH"), ("S2", "SWITCH"), ("F1", "FUSE"),
    ("ANT1", "ANTENNA"), ("BT1", "BATTERY"), ("MOD1", "MODULE"),
    ("TP1", None), ("K1", None), ("", None),
])
def test_refdes_prefix_is_the_fallback_for_unresolved_rows(refdes, expected):
    assert guess_category(_line(refdes, found=False)) == expected


def test_unknown_distributor_category_falls_back_to_refdes():
    assert guess_category(_line("U7", category="Sensors, Transducers > Temperature Sensors")) == "INTEGRATED CIRCUIT"


def test_nothing_known_stays_blank():
    assert guess_category(_line("K1", category="Relays > Signal Relays")) is None
