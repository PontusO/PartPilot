"""Guess the catalog category for a part the BOM importer is about to create.

Evidence, strongest first: the resolver's component type (it parsed the BOM text), the distributor's
category for the chosen part, and finally the RefDes prefix — the only clue left for rows nobody
resolved. The rules live in ``config/category_rules.yaml`` so the vocabulary can be tuned without
code. When nothing matches the category stays blank rather than guessing.
"""

from __future__ import annotations

import re

from partpilot.config import load_category_rules
from partpilot.models import CompType, ResolvedLine

_PREFIX = re.compile(r"[A-Za-z]+")


def _from_comp_type(line: ResolvedLine) -> str | None:
    if line.spec and line.spec.comp_type and line.spec.comp_type != CompType.OTHER:
        return line.spec.comp_type.value.upper()
    return None


def _from_distributor(path: str | None, rules: list) -> str | None:
    if not path:
        return None
    for pattern, category in rules:
        # Word-start match: "connector" hits "Connectors", but "ics" does not hit "Ceramic".
        if re.search(r"(?<![a-z0-9])" + re.escape(str(pattern)), path, re.IGNORECASE):
            return str(category)
    return None


def _from_refdes(refdes: list[str], table: dict[str, str]) -> str | None:
    for ref in refdes:
        m = _PREFIX.match(ref or "")
        if m:
            return table.get(m.group(0).upper())
    return None


def guess_category(line: ResolvedLine, rules: dict | None = None) -> str | None:
    rules = rules if rules is not None else load_category_rules()
    return (_from_comp_type(line)
            or _from_distributor(line.chosen.category if line.chosen else None, rules["distributor"])
            or _from_refdes(line.line.refdes, rules["refdes"]))
