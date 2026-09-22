"""Build the iLabs internal ``value`` notation for a part from its parsed spec + a
distributor match.

iLabs stores a passive's ``value`` as a slash-delimited spec string:

- **Resistor**:  ``<value>/<tolerance>/<power>/<package>``      e.g. ``1K/5%/0.0625W/0402``
- **Inductor**:  ``<value>/<tolerance>/<current>/<package>``    e.g. ``10uH/10%/300mA/0402``
- **Capacitor**: ``<value>/<voltage>/<tolerance>[/<tempco>]/<package>``
                 e.g. ``56pF/250V/5%/0603`` or ``56pF/250V/5%/C0G/0402`` (tempco optional)

Values use RKM notation for resistors (``4K7``, ``0R5``); capacitors/inductors keep the unit
(``56pF``, ``10uH``) since the ``F``/``H`` aids readability. Fields the BOM didn't carry are
filled from the distributor's parametric data (``Candidate.parameters``) — Digi-Key and Mouser
name and format those differently, so we try several keys and normalise the result. Any expected
field we still can't fill is returned in ``missing`` so the importer can flag the part for review
rather than silently writing a half-spec.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..models import Candidate, CompType, PartSpec
from .units import format_capacitance, format_inductance

# Distributor parameter keys, tried in order. Digi-Key and Mouser use different names.
_VOLTAGE_KEYS = ("Voltage - Rated", "Voltage Rating", "Voltage Rated", "Rated Voltage", "Voltage")
_TOLERANCE_KEYS = ("Tolerance",)
_POWER_KEYS = ("Power (Watts)", "Power Rating", "Power - Max", "Power Rating (Watts)", "Power")
_TEMPCO_KEYS = ("Temperature Coefficient", "Dielectric", "Temperature Coefficient (TC)")
_CURRENT_KEYS = ("Current Rating (Amps)", "Current Rating", "Current - Rating",
                 "Saturation Current", "Current Rating (A)", "Rated Current")
_PACKAGE_KEYS = ("Package / Case", "Supplier Device Package", "Package", "Case Code - Imperial")


@dataclass
class ValueResult:
    value: str                                  # the assembled slash notation
    missing: list[str] = field(default_factory=list)   # expected fields we couldn't fill


def format_resistance_rkm(ohms: float) -> str:
    """RKM (BS 1852) resistance code: the multiplier letter doubles as the decimal point.

    e.g. 0 -> '0R', 0.5 -> '0R5', 4.7 -> '4R7', 470 -> '470R', 1e3 -> '1K', 4.7e3 -> '4K7',
    1e6 -> '1M', 4.7e6 -> '4M7'. No trailing zeros (1 kΩ is '1K', not '1K0').
    """
    if ohms < 1e3:
        unit, scaled = "R", ohms
    elif ohms < 1e6:
        unit, scaled = "K", ohms / 1e3
    elif ohms < 1e9:
        unit, scaled = "M", ohms / 1e6
    else:
        unit, scaled = "G", ohms / 1e9
    s = f"{scaled:.3f}".rstrip("0").rstrip(".")   # "4.7", "1", "0.5", "470"
    if "." in s:
        whole, frac = s.split(".")
        return f"{whole}{unit}{frac}"             # 4.7 -> "4K7", 0.5 -> "0R5"
    return f"{s}{unit}"                            # 1 -> "1K", 470 -> "470R"


def _ascii(text: str | None) -> str | None:
    """Micro sign to plain 'u' (so 10µH reads as 10uH) and drop the ohm sign."""
    if text is None:
        return None
    return text.replace("µ", "u").replace("Ω", "")


def _param(params: dict[str, str], keys) -> str | None:
    for k in keys:
        v = (params.get(k) or "").strip()
        if v:
            return v
    return None


def _first_token(text: str) -> str:
    """Distributors pack alternates into one field ('0.0625W, 1/16W', 'C0G, NP0'); take the first."""
    return re.split(r"[,;/]", text)[0].strip()


def _norm_voltage(raw: str) -> str:
    v = raw.replace(" ", "")
    return v if v.upper().endswith("V") or "V" in v.upper() else f"{v}V"


def _norm_tolerance(raw: str) -> str:
    return raw.replace("±", "").replace(" ", "").strip()


def _norm_power(raw: str) -> str:
    return _first_token(raw).replace(" ", "")


def _norm_current(raw: str) -> str:
    return _first_token(raw).replace(" ", "")


def _norm_tempco(raw: str) -> str:
    return _first_token(raw).replace(" ", "")


def _norm_package(raw: str) -> str | None:
    """'0603 (1608 Metric)' -> '0603'; only accept a leading 4-digit imperial code."""
    m = re.match(r"\s*(\d{4})\b", raw)
    return m.group(1) if m else None


def _value_token(spec: PartSpec) -> str | None:
    """The leading value element, formatted per component family."""
    si = spec.value_si
    if spec.comp_type == CompType.RESISTOR and si is not None:
        return format_resistance_rkm(si)
    if spec.comp_type == CompType.CAPACITOR and si is not None:
        return _ascii(format_capacitance(si))
    if spec.comp_type == CompType.INDUCTOR and si is not None:
        return _ascii(format_inductance(si))
    return _ascii(spec.value_display) or _ascii(spec.value_raw)


def build_value_string(spec: PartSpec | None, candidate: Candidate | None) -> ValueResult | None:
    """Assemble the iLabs value notation for a passive. Returns None for non-passives (ICs,
    connectors, crystals…) where there's no parametric notation — the caller keeps the raw value.
    """
    if spec is None or spec.comp_type not in (CompType.RESISTOR, CompType.CAPACITOR,
                                              CompType.INDUCTOR):
        return None

    params = candidate.parameters if candidate else {}
    missing: list[str] = []

    def pick(spec_attr, keys, normaliser, label):
        raw = spec_attr or _param(params, keys)
        if not raw:
            missing.append(label)
            return None
        return normaliser(raw)

    value = _value_token(spec)
    if not value:
        missing.append("value")

    pkg = (spec.package_imperial
           or (_norm_package(_param(params, _PACKAGE_KEYS) or "")))
    package = pkg or None
    if not package:
        missing.append("package")

    tolerance = pick(spec.tolerance, _TOLERANCE_KEYS, _norm_tolerance, "tolerance")

    if spec.comp_type == CompType.CAPACITOR:
        voltage = pick(spec.voltage, _VOLTAGE_KEYS, _norm_voltage, "voltage")
        tempco = spec.dielectric or _param(params, _TEMPCO_KEYS)   # optional — not flagged
        tempco = _norm_tempco(tempco) if tempco else None
        fields = [value, voltage, tolerance, tempco, package]
    elif spec.comp_type == CompType.RESISTOR:
        power = pick(None, _POWER_KEYS, _norm_power, "power")
        fields = [value, tolerance, power, package]
    else:  # INDUCTOR
        current = pick(None, _CURRENT_KEYS, _norm_current, "current")
        fields = [value, tolerance, current, package]

    notation = "/".join(f for f in fields if f)
    return ValueResult(value=notation, missing=missing)
