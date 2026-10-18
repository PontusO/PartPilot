"""Column mapping for the Setup "Import parts from BOM" tool.

Reads a file's headers + sample values, pre-fills a mapping (remembered for this exact header
set, else the yaml alias guess), validates the user's choices, and remembers them in
``app_settings`` so the next upload of the same layout is pre-filled.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from partpilot.bom.normalize import _norm, detect_columns
from partpilot.bom.readers import read_table
from partpilot.config import load_column_mappings

from ...core.db import Database
from . import repo

# (field, label) — the BomLine fields the resolver understands.
TARGETS: list[tuple[str, str]] = [
    ("refdes", "RefDes"), ("qty", "Qty"), ("value", "Value"), ("device", "Device / MPN"),
    ("package", "Package"), ("description", "Description"), ("comment", "Comment"),
    ("manufacturer", "Manufacturer"),
]
TARGET_FIELDS = {f for f, _ in TARGETS}
IGNORE = "ignore"
SAMPLES = 3


def read_headers(path: Path) -> tuple[list[str], list[list[str]]]:
    """Headers and up to SAMPLES non-empty values per column. Raises ValueError if unreadable."""
    df = read_table(path)          # raises ValueError on an unsupported suffix
    headers = [str(c) for c in df.columns]
    if not headers:
        raise ValueError("No header row found in the file.")
    samples: list[list[str]] = []
    for col in df.columns:
        vals = [str(v).strip() for v in df[col].tolist()]
        samples.append([v for v in vals if v][:SAMPLES])
    return headers, samples


def header_key(headers: list[str]) -> str:
    """app_settings key for this exact header layout (case/punctuation-insensitive, order-aware)."""
    joined = "\x1f".join(_norm(h) for h in headers)
    return "bom_import.mapping." + hashlib.sha1(joined.encode("utf-8")).hexdigest()


def guess_mapping(headers: list[str]) -> dict[str, str]:
    return detect_columns(list(headers), load_column_mappings())


def remembered_mapping(db: Database, headers: list[str]) -> dict[str, str] | None:
    raw = repo.get_setting(db, header_key(headers))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    mapping = data.get("mapping") or {}
    # Only keep entries that still point at a real header + known field.
    return {f: h for f, h in mapping.items() if f in TARGET_FIELDS and h in headers} or None


def remember_mapping(db: Database, headers: list[str], mapping: dict[str, str]) -> None:
    payload = {"headers": list(headers), "mapping": dict(mapping),
               "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    repo.set_setting(db, header_key(headers), json.dumps(payload))


def prefill_mapping(db: Database, headers: list[str]) -> dict[str, str]:
    return remembered_mapping(db, headers) or guess_mapping(headers)


def parse_mapping(form_items: dict[str, str], headers: list[str]) -> dict[str, str]:
    """Turn ``map_<i> -> target`` form fields into ``{field: header}``.

    Raises ValueError with a message for the user when a target is used twice or neither
    Value nor Device / MPN is mapped.
    """
    labels = dict(TARGETS)
    mapping: dict[str, str] = {}
    for key, target in form_items.items():
        if not key.startswith("map_") or not key[4:].isdigit():
            continue
        idx = int(key[4:])
        if idx >= len(headers) or target not in TARGET_FIELDS:
            continue
        if target in mapping:
            raise ValueError(
                f"'{labels[target]}' is mapped more than once ({mapping[target]} and "
                f"{headers[idx]}). Each PartPilot field can take one column.")
        mapping[target] = headers[idx]
    if "value" not in mapping and "device" not in mapping:
        raise ValueError("Map at least one column to Value or Device / MPN.")
    return mapping


def run_mappings(mapping: dict[str, str]) -> dict[str, list[str]]:
    """The per-run ``mappings`` for ``resolve_bom_file``: only the fields the user mapped."""
    return {field: [header] for field, header in mapping.items()}
