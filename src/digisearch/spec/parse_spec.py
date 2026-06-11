"""Turn a generic (parametric) BOM line into a structured PartSpec + search query."""

from __future__ import annotations

import re

from ..config import Settings
from ..models import BomLine, CompType, LineKind, PartSpec
from .classify import comp_type_of
from .eseries import is_standard_resistance, nearest_standard_resistance
from .units import (
    format_capacitance,
    format_frequency,
    format_inductance,
    format_resistance,
    parse_frequency,
    parse_resistance,
    parse_rkm_value,
)

# Known imperial chip sizes and the metric codes that map to them.
_IMPERIAL = {"0201", "0402", "0603", "0805", "1206", "1210", "1812", "2010", "2512", "0806", "1008"}
_METRIC_TO_IMPERIAL = {
    "0603": "0201", "1005": "0402", "1608": "0603", "2012": "0805",
    "2016": "0806", "3216": "1206", "3225": "1210", "4532": "1812",
    "5025": "2010", "6332": "2512", "2520": "1008",
}


def extract_package(line: BomLine) -> str | None:
    """Derive an imperial chip size from the device/package strings."""
    for source in (line.device, line.package):
        if not source:
            continue
        # Imperial size directly present, e.g. CHIP-0402 or C0402.
        for m in re.findall(r"(\d{4})", source):
            if m in _IMPERIAL:
                return m
        # Metric code in parentheses, e.g. (1005-METRIC) or CAPC1005X60.
        for m in re.findall(r"(\d{4})", source):
            if m in _METRIC_TO_IMPERIAL:
                return _METRIC_TO_IMPERIAL[m]
    return None


def extract_dimension_code(line: BomLine) -> str | None:
    """Return a 4-digit outline-dimension code (e.g. crystal '3225' = 3.2 x 2.5 mm)."""
    for source in (line.device, line.package):
        if not source:
            continue
        m = re.search(r"(\d{4})", source)
        if m:
            return m.group(1)
    return None


def format_dimension_code(code: str) -> str:
    """'3225' -> '3.2 x 2.5 mm' (length x width, tenths of a millimetre)."""
    return f"{int(code[:2]) / 10:.1f} x {int(code[2:]) / 10:.1f} mm"


def build_spec(line: BomLine, settings: Settings, kind: LineKind) -> PartSpec:
    ctype = comp_type_of(line)
    spec = PartSpec(comp_type=ctype, value_raw=line.value, package_imperial=extract_package(line))
    value = (line.value or "").strip()

    if ctype == CompType.RESISTOR:
        ohms = parse_resistance(value)
        if ohms is not None:
            spec.value_si = ohms
            spec.value_display = format_resistance(ohms)
            if not is_standard_resistance(ohms):
                nearest = format_resistance(nearest_standard_resistance(ohms))
                spec.value_warning = f"non-standard value — nearest standard {nearest} (possible BOM error)"
        spec.tolerance = settings.default_resistor_tolerance
        spec.assumed.append("tolerance")
    elif ctype == CompType.CAPACITOR:
        # parse_rkm_value handles both standard (0.1uF, 15pF) and RKM (0u1, 1p5) notation.
        farads = parse_rkm_value(value, CompType.CAPACITOR)
        if farads is not None:
            spec.value_si = farads
            spec.value_display = format_capacitance(farads)
            small = farads <= 1e-9
            spec.dielectric = (
                settings.default_capacitor_dielectric_small
                if small
                else settings.default_capacitor_dielectric_bulk
            )
        else:
            spec.dielectric = settings.default_capacitor_dielectric_bulk
        spec.voltage = settings.default_capacitor_voltage
        spec.assumed.extend(["dielectric", "voltage"])
    elif ctype == CompType.INDUCTOR:
        henries = parse_rkm_value(value, CompType.INDUCTOR)
        if henries is not None:
            spec.value_si = henries
            spec.value_display = format_inductance(henries)
    elif ctype == CompType.CRYSTAL:
        hz = parse_frequency(value)
        if hz is not None:
            spec.value_si = hz
            spec.value_display = format_frequency(hz)
        # Crystals are specified by package outline (e.g. 3225 = 3.2 x 2.5 mm),
        # not an EIA chip size, so describe the dimensions instead of mapping to one.
        spec.package_imperial = None
        code = extract_dimension_code(line)
        if code:
            spec.package_code = code
            spec.package_note = format_dimension_code(code)

    if spec.value_display is None:
        spec.value_display = value or None
    return spec


def _ascii_query(text: str | None) -> str:
    """Digi-Key keyword search rejects 'Ω' and is happier with ASCII units."""
    return (text or "").replace("µ", "u").replace("Ω", "")


def _trim(x: float) -> str:
    return f"{x:.3f}".rstrip("0").rstrip(".")


def resistance_query_token(ohms: float) -> str:
    """ASCII value token for a resistor search.

    Digi-Key keyword search disambiguates sub-kΩ values only with RKM 'R' notation
    ('100R' -> 100 Ω; a spaced '100 ohm' wrongly surfaces 100 kΩ parts). e.g.
    0->'0 ohm', 100->'100R', 4.7->'4R7', 4700->'4.7k', 1e6->'1M'.
    """
    if ohms == 0:
        return "0 ohm"
    if ohms < 1000:
        s = _trim(ohms)
        return s.replace(".", "R") if "." in s else f"{s}R"
    if ohms < 1e6:
        return f"{_trim(ohms / 1e3)}k"
    return f"{_trim(ohms / 1e6)}M"


def build_query(spec: PartSpec) -> str:
    parts: list[str] = []
    pkg = spec.package_imperial
    if spec.comp_type == CompType.RESISTOR:
        if spec.value_si is not None:
            parts.append(resistance_query_token(spec.value_si))
        else:
            parts.append(_ascii_query(spec.value_display))
        if pkg:
            parts.append(pkg)
        parts.append("resistor")
        if spec.tolerance and spec.value_si != 0:  # a 0 Ω jumper has no tolerance spec
            parts.append(spec.tolerance)
    elif spec.comp_type == CompType.CAPACITOR:
        parts.append(_ascii_query(spec.value_display))
        if pkg:
            parts.append(pkg)
        if spec.dielectric:
            parts.append(spec.dielectric)
        parts.append("MLCC capacitor")
    elif spec.comp_type == CompType.INDUCTOR:
        parts.append(_ascii_query(spec.value_display))
        if pkg:
            parts.append(pkg)
        parts.append("inductor")
    elif spec.comp_type == CompType.CRYSTAL:
        parts.append(_ascii_query(spec.value_display))
        parts.append("crystal")
        if spec.package_note:
            parts.append(spec.package_note)
    else:
        parts.append(_ascii_query(spec.value_display) or spec.value_raw or "")
    return " ".join(p for p in parts if p).strip()


def mpn_query(line: BomLine) -> str:
    """Search string for a real-MPN line: prefer the value if it looks like a part number."""
    from .classify import looks_like_mpn

    value = (line.value or "").strip()
    device = (line.device or "").strip()
    if looks_like_mpn(value):
        return value
    if looks_like_mpn(device):
        return device
    return value or device


def relaxed_mpn_queries(mpn: str, max_q: int = 3) -> list[str]:
    """Progressively trimmed fallbacks when an exact MPN search returns nothing.

    e.g. 'SL3401A-TP' -> ['SL3401A', 'SL3401'];  'ADV7513BSWZ' -> ['ADV7513'].
    """
    base = re.sub(r"\s+", "", mpn.strip())
    out: list[str] = []
    cur = base
    for _ in range(5):
        if "-" in cur:
            nxt = cur.rsplit("-", 1)[0]
        else:
            m = re.search(r"^(.*?\d)[A-Za-z]+$", cur)  # drop trailing version/packaging letters
            nxt = m.group(1) if m else None
        if not nxt or nxt == cur or len(nxt) < 4:
            break
        out.append(nxt)
        cur = nxt
        if len(out) >= max_q:
            break
    return out
