"""Standard IEC 60063 E-series checks, to catch non-standard (likely typo) values."""

from __future__ import annotations

from math import floor, log10

# E24 mantissa × 10
_E24 = {
    10, 11, 12, 13, 15, 16, 18, 20, 22, 24, 27, 30,
    33, 36, 39, 43, 47, 51, 56, 62, 68, 75, 82, 91,
}
# E96 mantissa × 100
_E96 = {
    100, 102, 105, 107, 110, 113, 115, 118, 121, 124, 127, 130,
    133, 137, 140, 143, 147, 150, 154, 158, 162, 165, 169, 174,
    178, 182, 187, 191, 196, 200, 205, 210, 215, 221, 226, 232,
    237, 243, 249, 255, 261, 267, 274, 280, 287, 294, 301, 309,
    316, 324, 332, 340, 348, 357, 365, 374, 383, 392, 402, 412,
    422, 432, 442, 453, 464, 475, 487, 499, 511, 523, 536, 549,
    562, 576, 590, 604, 619, 634, 649, 665, 681, 698, 715, 732,
    750, 768, 787, 806, 825, 845, 866, 887, 909, 931, 953, 976,
}


def _mantissa(value: float) -> float:
    """Normalize to [1, 10)."""
    return value / 10 ** floor(log10(value))


_TOL = 0.005  # within 0.5% of a series value counts as that value (absorbs float error)


def is_standard_resistance(ohms: float) -> bool:
    """True if ``ohms`` is a standard E24 or E96 value (0 Ω jumpers count as standard).

    Note: genuine E192-only values are not recognised and would be flagged.
    """
    if ohms <= 0:
        return True
    m = _mantissa(ohms)
    candidates = [v / 10 for v in _E24] + [v / 100 for v in _E96]
    return min(abs(c - m) for c in candidates) / m <= _TOL


def nearest_standard_resistance(ohms: float) -> float:
    """Closest E96 value (with the original decade), for suggesting a correction."""
    if ohms <= 0:
        return ohms
    decade = 10 ** floor(log10(ohms))
    m = _mantissa(ohms)
    best = min(_E96, key=lambda v: abs(v / 100 - m))
    return best / 100 * decade
