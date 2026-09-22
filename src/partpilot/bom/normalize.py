"""Map a raw BOM table onto canonical BomLine records."""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz

from ..config import load_column_mappings
from ..models import BomLine
from ..util.refdes import expand_refdes
from .readers import read_table


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name).lower())


def detect_columns(columns: list[str], mappings: dict) -> dict[str, str]:
    """Resolve canonical field -> actual source column using alias + fuzzy match."""
    normalized = {col: _norm(col) for col in columns}
    resolved: dict[str, str] = {}
    used: set[str] = set()
    for field, aliases in mappings.items():
        alias_norms = [_norm(a) for a in aliases]
        # 1) exact alias hit
        match = next(
            (c for c, n in normalized.items() if n in alias_norms and c not in used), None
        )
        # 2) fuzzy fallback
        if match is None:
            best, best_score = None, 0
            for col, n in normalized.items():
                if col in used:
                    continue
                score = max(fuzz.ratio(n, a) for a in alias_norms)
                if score > best_score:
                    best, best_score = col, score
            if best_score >= 85:
                match = best
        if match is not None:
            resolved[field] = match
            used.add(match)
    return resolved


def normalize_frame(df: pd.DataFrame, mappings: dict | None = None) -> list[BomLine]:
    mappings = mappings or load_column_mappings()
    colmap = detect_columns(list(df.columns), mappings)

    def cell(row: pd.Series, field: str) -> str | None:
        src = colmap.get(field)
        if not src:
            return None
        val = row.get(src)
        if val is None:
            return None
        s = str(val).strip()
        return s or None

    lines: list[BomLine] = []
    for idx, row in df.iterrows():
        refdes_raw = cell(row, "refdes")
        refdes = expand_refdes(refdes_raw)
        qty_raw = cell(row, "qty")
        qty = len(refdes)
        if qty == 0 and qty_raw:
            try:
                qty = int(float(qty_raw))
            except ValueError:
                qty = 0
        line = BomLine(
            refdes=refdes,
            qty=qty,
            value=cell(row, "value"),
            device=cell(row, "device"),
            package=cell(row, "package"),
            description=cell(row, "description"),
            comment=cell(row, "comment"),
            row_index=int(idx),
            raw={k: ("" if pd.isna(v) else str(v)) for k, v in row.items()},
        )
        if not any([line.refdes, line.value, line.device]):
            continue  # skip empty/separator rows
        lines.append(line)
    return lines


def load_bom(path: str | Path, mappings: dict | None = None) -> list[BomLine]:
    return normalize_frame(read_table(path), mappings)
