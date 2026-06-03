import math

import pytest

from digisearch.spec.units import (
    format_capacitance,
    format_resistance,
    parse_capacitance,
    parse_frequency,
    parse_inductance,
    parse_resistance,
)
from digisearch.util.refdes import expand_refdes, refdes_count


@pytest.mark.parametrize(
    "text,ohms",
    [
        ("0R", 0.0), ("10R", 10.0), ("100R", 100.0), ("220R", 220.0),
        ("4K7", 4700.0), ("5K1", 5100.0), ("10K", 10000.0), ("100K", 100000.0),
        ("448K", 448000.0), ("2K", 2000.0), ("887", 887.0), ("2.2k", 2200.0),
    ],
)
def test_parse_resistance(text, ohms):
    assert parse_resistance(text) == pytest.approx(ohms)


@pytest.mark.parametrize(
    "text,farads",
    [("0.1uF", 0.1e-6), ("2.2uF", 2.2e-6), ("15pF", 15e-12), ("22pF", 22e-12),
     ("10uF", 10e-6), ("4.7uF", 4.7e-6)],
)
def test_parse_capacitance(text, farads):
    assert parse_capacitance(text) == pytest.approx(farads)


def test_parse_inductance_and_frequency():
    assert parse_inductance("10uH") == pytest.approx(10e-6)
    assert parse_inductance("2.2uH") == pytest.approx(2.2e-6)
    assert parse_frequency("12MHz") == pytest.approx(12e6)


def test_format_roundtrip():
    assert format_resistance(4700) == "4.7kΩ"
    assert format_resistance(0) == "0Ω"
    assert format_capacitance(0.1e-6) == "100nF"
    assert format_capacitance(22e-12) == "22pF"


def test_parse_param_value_with_units():
    # values as Digi-Key returns them in Parameters
    assert parse_resistance("10 k") == pytest.approx(10000)
    assert parse_capacitance("0.1 µF") == pytest.approx(0.1e-6)


def test_expand_refdes_ranges_and_lists():
    assert expand_refdes("R1-R4, R7") == ["R1", "R2", "R3", "R4", "R7"]
    assert expand_refdes("TP3, TP4, TP5") == ["TP3", "TP4", "TP5"]
    assert refdes_count("C4, C7, C8, C9") == 4
    assert expand_refdes("") == []
    assert expand_refdes(None) == []
