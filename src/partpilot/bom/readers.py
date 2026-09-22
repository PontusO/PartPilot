"""Read raw BOM tables from various formats into a uniform string DataFrame.

Supports Excel (.xlsx/.xls) and delimited text (.csv/.tsv/.txt) with automatic
delimiter sniffing and header-row detection. EAGLE exports (semicolon-delimited,
quoted, header on the first row) and KiCad/Altium CSV exports all flow through here.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pandas as pd

# Keywords used to locate the header row when files carry title/metadata rows above it.
_HEADER_HINTS = {
    "qty", "quantity", "value", "device", "package", "footprint",
    "parts", "refdes", "reference", "designator", "description",
}


def _detect_delimiter(sample: str) -> str:
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t|")
        return dialect.delimiter
    except csv.Error:
        # Fall back to whichever common delimiter appears most often.
        counts = {d: sample.count(d) for d in [";", ",", "\t", "|"]}
        return max(counts, key=counts.get)


def _find_header_row(rows: list[list[str]]) -> int:
    best_idx, best_hits = 0, -1
    for idx, row in enumerate(rows[:15]):
        cells = {str(c).strip().lower() for c in row}
        hits = len(cells & _HEADER_HINTS)
        if hits > best_hits:
            best_idx, best_hits = idx, hits
    return best_idx


def _frame_from_rows(rows: list[list[str]]) -> pd.DataFrame:
    rows = [r for r in rows if any(str(c).strip() for c in r)]  # drop blank rows
    if not rows:
        return pd.DataFrame()
    header_idx = _find_header_row(rows)
    header = [str(c).strip() for c in rows[header_idx]]
    body = rows[header_idx + 1 :]
    width = len(header)
    norm = [(r + [""] * width)[:width] for r in body]
    df = pd.DataFrame(norm, columns=header)
    # Drop fully-unnamed/empty trailing columns common in EAGLE exports.
    df = df.loc[:, [bool(str(c).strip()) for c in df.columns]]
    return df.map(lambda v: "" if v is None else str(v).strip())


def read_csv(path: Path) -> pd.DataFrame:
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    delimiter = _detect_delimiter(text[:4096])
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    return _frame_from_rows([list(r) for r in reader])


def read_excel(path: Path) -> pd.DataFrame:
    raw = pd.read_excel(path, header=None, dtype=str, engine="openpyxl")
    rows = raw.where(pd.notna(raw), "").values.tolist()
    return _frame_from_rows([[str(c) for c in r] for r in rows])


def read_table(path: str | Path) -> pd.DataFrame:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm", ".xls"):
        return read_excel(path)
    if suffix in (".csv", ".tsv", ".txt"):
        return read_csv(path)
    raise ValueError(f"Unsupported BOM format: {suffix} ({path.name})")
