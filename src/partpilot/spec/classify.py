"""Decide how each BOM line should be sourced (its LineKind)."""

from __future__ import annotations

import re

from ..config import Settings
from ..models import BomLine, CompType, LineKind

_NON_ORDERABLE = re.compile(
    r"test\s*point|testpoint|mount[\s_-]*hole|mounting\s*hole|fiducial|"
    r"test\s*pad|^tpb|^tp[_-]|drill|keepout|logo",
    re.IGNORECASE,
)
_CRYSTAL_DEV = re.compile(r"xtal|crystal|resonator", re.IGNORECASE)
_PASSIVE_DEV = re.compile(r"^(?P<t>[RCL])[_-](CHIP|EU)|^(?P<t2>[RCL])-EU", re.IGNORECASE)
# RKM resistor code (0R, 4K7, 100K) or an explicit unit value (0.1uF, 10uH, 12MHz).
_PASSIVE_VALUE = re.compile(r"^\d*[RKMG]\d*$|^\d+(\.\d+)?\s*[pnuµm]?[FH]z?$", re.IGNORECASE)


def comp_type_of(line: BomLine) -> CompType:
    text = " ".join(filter(None, [line.device, line.description])).lower()
    if _CRYSTAL_DEV.search(text):
        return CompType.CRYSTAL
    if re.search(r"\bc[_-](chip|eu)|capacitor|\bcap\b", text):
        return CompType.CAPACITOR
    if re.search(r"\br[_-](chip|eu)|resistor|\bres\b", text):
        return CompType.RESISTOR
    if re.search(r"\bl[_-](chip|eu)|inductor|\bind\b", text):
        return CompType.INDUCTOR
    return CompType.OTHER


def looks_like_mpn(text: str | None) -> bool:
    """Heuristic: alphanumeric token with both letters and digits, length >= 4."""
    if not text:
        return False
    t = text.strip()
    if len(t) < 4:
        return False
    if _PASSIVE_VALUE.match(t):  # a component value, not a part number
        return False
    has_alpha = any(c.isalpha() for c in t)
    has_digit = any(c.isdigit() for c in t)
    # Pure numeric order codes (e.g. Würth 68711014022) are also valid MPNs.
    if t.isdigit() and len(t) >= 8:
        return True
    return has_alpha and has_digit


def classify(line: BomLine, settings: Settings) -> LineKind:
    value = (line.value or "").strip()
    device = (line.device or "").strip()

    if value.upper() in {v.upper() for v in settings.dnp_values}:
        return LineKind.DNP

    blob = " ".join(filter(None, [value, device, line.package, line.description]))
    if _NON_ORDERABLE.search(blob):
        return LineKind.NON_ORDERABLE

    ctype = comp_type_of(line)
    is_chip_passive = bool(_PASSIVE_DEV.search(device)) or "generic" in (line.description or "").lower()

    if ctype == CompType.CRYSTAL:
        return LineKind.CRYSTAL
    if ctype in (CompType.RESISTOR, CompType.CAPACITOR, CompType.INDUCTOR) and is_chip_passive:
        return LineKind.GENERIC_PASSIVE

    # Anything left that carries a real part number -> MPN search.
    if looks_like_mpn(value) or looks_like_mpn(device):
        return LineKind.MPN
    # Fallback: passive type but odd device (e.g. C-EUC0402) still treated parametrically.
    if ctype in (CompType.RESISTOR, CompType.CAPACITOR, CompType.INDUCTOR):
        return LineKind.GENERIC_PASSIVE
    return LineKind.MPN
