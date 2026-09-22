"""Reference-designator parsing and expansion."""

from __future__ import annotations

import re

_RANGE = re.compile(r"^([A-Za-z]+)(\d+)\s*[-–]\s*([A-Za-z]+)?(\d+)$")
_SINGLE = re.compile(r"^([A-Za-z]+)(\d+)$")


def expand_refdes(raw: str | None) -> list[str]:
    """Expand a designator field into a flat, ordered list.

    Handles comma/whitespace separated lists and ranges such as
    ``"R1-R4, R7"`` or ``"TP3 - TP6"`` -> ``[R1, R2, R3, R4, R7]``.
    Tokens that are not parseable as ``<prefix><number>`` are kept verbatim.
    """
    if not raw:
        return []
    out: list[str] = []
    for token in re.split(r"[,;]+", str(raw)):
        token = token.strip()
        if not token:
            continue
        m = _RANGE.match(token)
        if m:
            prefix, start, end_prefix, end = m.groups()
            end_prefix = end_prefix or prefix
            if end_prefix == prefix and int(end) >= int(start):
                out.extend(f"{prefix}{n}" for n in range(int(start), int(end) + 1))
                continue
        out.append(token)
    return out


def refdes_count(raw: str | None) -> int:
    return len(expand_refdes(raw))
