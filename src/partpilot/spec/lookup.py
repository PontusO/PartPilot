"""User-maintained lookup that maps generic EAGLE device names to real searches."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..models import BomLine
from .parse_spec import extract_package


@dataclass
class LookupRule:
    device_re: re.Pattern | None
    value_re: re.Pattern | None
    mpn: str | None
    query: str | None


def load_lookup(path: str | Path | None) -> list[LookupRule]:
    if path is None:
        from ..config import DEFAULT_CONFIG_DIR

        path = DEFAULT_CONFIG_DIR / "device_lookup.yaml"
    path = Path(path)
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    rules: list[LookupRule] = []
    for raw in data.get("rules", []):
        rules.append(
            LookupRule(
                device_re=re.compile(raw["device"], re.IGNORECASE) if raw.get("device") else None,
                value_re=re.compile(raw["value"], re.IGNORECASE) if raw.get("value") else None,
                mpn=raw.get("mpn"),
                query=raw.get("query"),
            )
        )
    return rules


def match_lookup(line: BomLine, rules: list[LookupRule]) -> LookupRule | None:
    device = line.device or ""
    value = line.value or ""
    for rule in rules:
        if rule.device_re and not rule.device_re.search(device):
            continue
        if rule.value_re and not rule.value_re.search(value):
            continue
        if rule.device_re is None and rule.value_re is None:
            continue  # a rule must constrain on something
        return rule
    return None


def render(template: str, line: BomLine) -> str:
    package = extract_package(line) or line.package or ""
    text = template.format(value=line.value or "", device=line.device or "", package=package)
    return re.sub(r"\s+", " ", text).strip()
