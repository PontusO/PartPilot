import pytest

from digisearch.config import Settings
from digisearch.models import BomLine, LineKind
from digisearch.spec.eseries import (
    is_standard_resistance,
    nearest_standard_resistance,
)
from digisearch.spec.parse_spec import build_spec

S = Settings()


@pytest.mark.parametrize("ohms", [0, 10, 100, 4700, 5100, 887, 100000, 453000, 470000])
def test_standard_values_pass(ohms):
    assert is_standard_resistance(ohms)


@pytest.mark.parametrize("ohms", [448000, 448, 449000, 126000])
def test_nonstandard_values_flagged(ohms):
    assert not is_standard_resistance(ohms)


def test_nearest_standard_suggests_453k_for_448k():
    assert nearest_standard_resistance(448000) == pytest.approx(453000)


def test_build_spec_flags_nonstandard_resistor():
    line = BomLine(value="448K", device="R_CHIP-0402(1005-METRIC)")
    spec = build_spec(line, S, LineKind.GENERIC_PASSIVE)
    assert spec.value_warning is not None and "453" in spec.value_warning


def test_build_spec_no_warning_for_standard_resistor():
    line = BomLine(value="453K", device="R_CHIP-0402(1005-METRIC)")
    spec = build_spec(line, S, LineKind.GENERIC_PASSIVE)
    assert spec.value_warning is None
