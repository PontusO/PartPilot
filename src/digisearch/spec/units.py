"""Parse electronic component values into SI quantities and tidy display strings."""

from __future__ import annotations

import re

from ..models import CompType

# --- Resistance (RKM / BS 1852 code: R, K, M used as decimal point + multiplier) ---

_R_MULT = {"R": 1.0, "K": 1e3, "M": 1e6, "G": 1e9}
_R_CODE = re.compile(r"^(\d*)([RKMG])(\d*)$", re.IGNORECASE)
_R_PLAIN = re.compile(r"^(\d+(?:\.\d+)?)\s*([RKMG]?)(?:Ω|ohms?)?$", re.IGNORECASE)


def parse_resistance(text: str) -> float | None:
    """Return ohms for forms like ``4K7``, ``5K1``, ``0R``, ``100K``, ``887``, ``2.2k``."""
    if not text:
        return None
    t = text.strip().replace("Ω", "").replace("ohm", "").replace("OHM", "").strip()
    m = _R_CODE.match(t)
    if m:
        whole, letter, frac = m.groups()
        mult = _R_MULT[letter.upper()]
        value = float(f"{whole or 0}.{frac or 0}")
        return value * mult
    m = _R_PLAIN.match(t)
    if m:
        num, letter = m.groups()
        mult = _R_MULT.get(letter.upper(), 1.0) if letter else 1.0
        return float(num) * mult
    return None


# --- Capacitance / Inductance / Frequency (SI prefixes) ---

_SI = {
    "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3,
    "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9,
}
_NUM_UNIT = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*([pnuµmkKMG]?)\s*(F|H|Hz|HZ|hz)?\s*$"
)


def _parse_si(text: str, unit: str) -> float | None:
    if not text:
        return None
    m = _NUM_UNIT.match(text.strip())
    if not m:
        return None
    num, prefix, found_unit = m.groups()
    if found_unit and found_unit.upper().rstrip("Z").rstrip("H") not in {unit.upper(), ""}:
        # crude guard: unit letter present but mismatched (e.g. F where we want H)
        if unit.upper() not in found_unit.upper():
            return None
    mult = _SI.get(prefix, 1.0) if prefix else 1.0
    return float(num) * mult


def parse_capacitance(text: str) -> float | None:
    """Farads for ``0.1uF``, ``15pF``, ``2.2uF``, ``22uF``."""
    return _parse_si(text, "F")


def parse_inductance(text: str) -> float | None:
    """Henries for ``10uH``, ``2.2uH``."""
    return _parse_si(text, "H")


def parse_frequency(text: str) -> float | None:
    """Hertz for ``12MHz``, ``32.768kHz``."""
    return _parse_si(text, "Hz")


_FREQ_IN_TEXT = re.compile(r"(\d+(?:\.\d+)?)\s*([kKMG]?)Hz", re.IGNORECASE)


def extract_frequency(text: str) -> float | None:
    """Find the first frequency inside free text (e.g. a crystal description).

    ``'12MHz Crystal 12pF SMD3225'`` -> 12e6; ``'CRYSTAL 32.7680KHZ 9PF'`` -> 32768.0.
    Only kHz/MHz/GHz prefixes (crystals are never sub-kHz), so the case of the prefix is
    normalized to avoid confusing ``M`` (mega) with ``m`` (milli).
    """
    m = _FREQ_IN_TEXT.search(text or "")
    if not m:
        return None
    num, prefix = m.groups()
    mult = {"K": 1e3, "M": 1e6, "G": 1e9}.get(prefix.upper(), 1.0)
    return float(num) * mult


# --- Display formatting ---

def _eng(value: float, prefixes: list[tuple[float, str]], unit: str) -> str:
    for factor, prefix in prefixes:
        if abs(value) >= factor:
            scaled = value / factor
            s = f"{scaled:.3f}".rstrip("0").rstrip(".")
            return f"{s}{prefix}{unit}"
    s = f"{value:.3g}"
    return f"{s}{unit}"


def format_resistance(ohms: float) -> str:
    if ohms == 0:
        return "0Ω"
    return _eng(ohms, [(1e9, "G"), (1e6, "M"), (1e3, "k"), (1.0, "")], "Ω")


def format_capacitance(farads: float) -> str:
    return _eng(farads, [(1.0, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n"), (1e-12, "p")], "F")


def format_inductance(henries: float) -> str:
    return _eng(henries, [(1.0, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n")], "H")


def format_frequency(hz: float) -> str:
    return _eng(hz, [(1e9, "G"), (1e6, "M"), (1e3, "k"), (1.0, "")], "Hz")


# --- Unified RKM parser (handles both EAGLE and miniMRP notations) ---

# Letter used as a decimal point + multiplier, per component family.
_RES_RKM = {"R": 1.0, "K": 1e3, "k": 1e3, "M": 1e6, "G": 1e9}
_CAPIND_RKM = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3}
# SI prefix used in standard notation (e.g. 4.7k, 15pF).
_SI_PREFIX = {
    "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3,
    "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9,
}


def parse_rkm_value(token: str, comp_type: CompType) -> float | None:
    """Parse a resistor/capacitor/inductor value in RKM **or** standard notation.

    Resistors: ``0R``, ``3R3``, ``4K7``, ``100K``, ``887``, ``4.7k``.
    Caps/inductors: ``1p5``, ``0p8``, ``3n3``, ``10uF``, ``0.1uF``, ``15pF``.
    Returns SI (ohms / farads / henries), or ``None`` if unparseable.
    """
    if not token:
        return None
    t = token.strip()
    rkm = _RES_RKM if comp_type == CompType.RESISTOR else _CAPIND_RKM
    # RKM form: <digits><letter><digits>, letter is the decimal point.
    m = re.match(rf"^(\d*)([{''.join(rkm)}])(\d*)", t)
    if m:
        whole, letter, frac = m.groups()
        return float(f"{whole or 0}.{frac or 0}") * rkm[letter]
    # Standard form: <number>[prefix][unit].
    m2 = re.match(r"^(\d*\.?\d+)\s*([pnuµmkKMG]?)", t)
    if m2:
        num, prefix = m2.groups()
        if prefix:
            mult = _SI_PREFIX.get(prefix)
            if mult is None:
                return None
        elif comp_type == CompType.RESISTOR:
            mult = 1.0  # bare number = ohms
        else:
            return None  # caps/inductors need a unit to be meaningful
        return float(num) * mult
    return None
