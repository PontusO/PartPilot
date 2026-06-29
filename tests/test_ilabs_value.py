"""Tests for the iLabs value-notation formatter (spec + distributor params -> slash string)."""

from digisearch.models import Candidate, CompType, PartSpec
from digisearch.spec.ilabs_value import build_value_string, format_resistance_rkm


def _cand(**params):
    return Candidate(supplier="Digi-Key", parameters=params)


def test_rkm_resistance_codes():
    cases = {0: "0R", 0.5: "0R5", 4.7: "4R7", 47: "47R", 470: "470R",
             1e3: "1K", 2.2e3: "2K2", 4.7e3: "4K7", 10e3: "10K",
             1e6: "1M", 4.7e6: "4M7"}
    for ohms, expected in cases.items():
        assert format_resistance_rkm(ohms) == expected


def test_resistor_full_notation_from_distributor_power():
    # The BOM gave value+tolerance+package; power comes only from the distributor parametric.
    spec = PartSpec(comp_type=CompType.RESISTOR, value_si=1e3, tolerance="5%",
                    package_imperial="0402")
    r = build_value_string(spec, _cand(**{"Power (Watts)": "0.0625W, 1/16W"}))
    assert r.value == "1K/5%/0.0625W/0402" and r.missing == []


def test_capacitor_with_optional_tempco():
    spec = PartSpec(comp_type=CompType.CAPACITOR, value_si=56e-12, tolerance="5%",
                    voltage="250V", dielectric="C0G", package_imperial="0402")
    r = build_value_string(spec, _cand())
    assert r.value == "56pF/250V/5%/C0G/0402" and r.missing == []


def test_capacitor_without_tempco_is_not_flagged():
    # Tempco/dielectric is optional — absent means a 4-field cap string, no review flag.
    spec = PartSpec(comp_type=CompType.CAPACITOR, value_si=100e-9, tolerance="10%",
                    voltage="50V", package_imperial="0603")
    r = build_value_string(spec, _cand())
    assert r.value == "100nF/50V/10%/0603" and r.missing == []


def test_inductor_current_rating():
    spec = PartSpec(comp_type=CompType.INDUCTOR, value_si=10e-6, tolerance="10%",
                    package_imperial="0402")
    r = build_value_string(spec, _cand(**{"Current Rating (Amps)": "300mA"}))
    assert r.value == "10uH/10%/300mA/0402"


def test_missing_voltage_is_reported():
    # Cap with no voltage anywhere -> built from what we have, voltage listed as missing.
    spec = PartSpec(comp_type=CompType.CAPACITOR, value_si=56e-12, tolerance="5%",
                    package_imperial="0402")
    r = build_value_string(spec, _cand())
    assert "voltage" in r.missing
    assert r.value == "56pF/5%/0402"      # separators collapse around the gap


def test_distributor_fills_voltage_and_tolerance_and_package():
    # BOM carried only the value; everything else comes from Digi-Key parametrics.
    spec = PartSpec(comp_type=CompType.CAPACITOR, value_si=56e-12)
    r = build_value_string(spec, _cand(**{
        "Voltage - Rated": "250 V", "Tolerance": "±5%", "Package / Case": "0603 (1608 Metric)"}))
    assert r.value == "56pF/250V/5%/0603" and r.missing == []


def test_mouser_parameter_names():
    spec = PartSpec(comp_type=CompType.RESISTOR, value_si=4.7e3)
    r = build_value_string(spec, _cand(**{
        "Tolerance": "1%", "Power Rating": "0.1W", "Package": "0805"}))
    assert r.value == "4K7/1%/0.1W/0805"


def test_non_passive_returns_none():
    # ICs/connectors have no parametric notation; caller keeps the raw value.
    assert build_value_string(PartSpec(comp_type=CompType.OTHER, value_raw="STM32"), None) is None
    assert build_value_string(None, None) is None
