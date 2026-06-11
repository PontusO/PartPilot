"""Read a miniMRP Access (.mdb) database and index its stock for matching.

miniMRP stores its data in a Microsoft Access (JET) file (``Data/mrp5data``). The
numeric columns are Access Decimal, which only ``mdbtools`` decodes reliably, so we
shell out to ``mdb-export``. Install it once with ``sudo apt install mdbtools``.
"""

from __future__ import annotations

import csv
import io
import re
import shutil
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from ..models import CompType
from ..spec.units import extract_frequency, parse_rkm_value

_CATEGORY = {
    "RESISTOR": CompType.RESISTOR,
    "CAPACITOR": CompType.CAPACITOR,
    "INDUCTOR": CompType.INDUCTOR,
    "CRYSTAL": CompType.CRYSTAL,
}
_IMPERIAL = {"0201", "0402", "0603", "0805", "1206", "1210", "1812", "2010", "2512", "0806", "1008"}
_VALUE_TOL = 0.02  # passives within 2% are the same nominal value
_CRYSTAL_TOL = 0.005  # crystals must match frequency tightly (0.5%)

# Common SMD crystal outline codes (length/width in tenths of a mm), e.g. 3225 = 3.2 x 2.5 mm.
_CRYSTAL_CODES = {"1610", "1612", "2012", "2016", "2520", "3215", "3225", "5032", "6035", "7050"}
_CRYSTAL_DIMS = re.compile(r"(\d\.\d)\s*(?:mm)?\s*[x×]\s*(\d\.\d)\s*mm?", re.IGNORECASE)


def _norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _crystal_package(text: str) -> str | None:
    """Pull a crystal outline code from free text: 'SMD3225-4P' or '3.2mm x 2.5mm' -> '3225'."""
    if not text:
        return None
    m = _CRYSTAL_DIMS.search(text)
    if m:
        return m.group(1).replace(".", "") + m.group(2).replace(".", "")
    for code in re.findall(r"\d{4}", text):
        if code in _CRYSTAL_CODES:
            return code
    return None


def _is_stem_prefix(stock_raw: str, stem_norm: str) -> bool:
    """Does ``stock_raw`` extend ``stem_norm`` at a natural boundary?

    Matches the stem against the stocked MPN's alphanumerics (ignoring separators), then
    checks the character in the *original* string immediately after the stem. A match is
    valid when that next character is a **separator** (``XC6565`` → ``XC6565-12``) or a
    **letter** (``MBR120`` → ``MBR120LSF``), but not a **digit** continuing the same number
    (``MBR120`` → ``MBR1200``). Keeping separators is what makes hyphenated suffixes work.
    """
    i = 0
    for pos, ch in enumerate(stock_raw):
        if not ch.isalnum():
            continue
        if i >= len(stem_norm) or ch.lower() != stem_norm[i]:
            return False
        i += 1
        if i == len(stem_norm):
            rest = stock_raw[pos + 1:]
            if not rest:
                return False  # same length -> not a fuller part
            nxt = rest[0]
            return (not nxt.isalnum()) or (not nxt.isdigit())
    return False


@dataclass
class StockItem:
    item_id: int
    master_pno: str
    mfr_pno: str
    name: str
    description: str
    category: str
    comp_type: CompType
    value_si: float | None
    package: str | None
    on_hand: float
    allocated: float
    on_order: float

    @property
    def free(self) -> float:
        return max(0.0, self.on_hand - self.allocated)

    @property
    def label(self) -> str:
        return self.master_pno or self.mfr_pno or self.name

    def matches_mpn(self, *identifiers: str | None) -> bool:
        """True if any identifier equals this item's MPN exactly (normalized)."""
        keys = {_norm(self.master_pno), _norm(self.mfr_pno)} - {""}
        return any(_norm(i) in keys for i in identifiers if i)


@dataclass
class StockIndex:
    items: list[StockItem]
    by_mpn: dict[str, StockItem] = field(default_factory=dict)
    by_param: dict[tuple, list[StockItem]] = field(default_factory=lambda: defaultdict(list))

    @classmethod
    def build(cls, items: list[StockItem]) -> "StockIndex":
        idx = cls(items=items)
        for it in items:
            for key in (it.master_pno, it.mfr_pno):
                if key:
                    idx.by_mpn.setdefault(_norm(key), it)
            if it.value_si is not None:
                idx.by_param[(it.comp_type, it.package)].append(it)
        return idx

    def match_mpn(self, *identifiers: str | None) -> StockItem | None:
        for ident in identifiers:
            hit = self.by_mpn.get(_norm(ident)) if ident else None
            if hit:
                return hit
        return None

    def match_mpn_prefix(self, *identifiers: str | None, min_len: int = 4) -> StockItem | None:
        """Match a partial/generic MPN against a fuller stocked MPN.

        Treats the identifier as a *stem* and matches a stocked MPN that extends it at a
        natural boundary — a separator (``XC6565`` → ``XC6565-12``) or a letter suffix
        (``MBR120`` → ``MBR120LSF``), but not a digit continuing the number (``MBR120`` →
        ``MBR1200``). See :func:`_is_stem_prefix`. Ambiguous stems resolve to the candidate
        with the most free stock; short stems (< ``min_len``) are ignored.
        """
        for ident in identifiers:
            stem = _norm(ident)
            if not ident or len(stem) < min_len:
                continue
            best, best_free = None, -1.0
            for it in self.items:
                for raw in (it.master_pno, it.mfr_pno):
                    if raw and _is_stem_prefix(raw, stem) and it.free > best_free:
                        best, best_free = it, it.free
            if best is not None:
                return best
        return None

    def match_crystal(
        self, value_si: float | None, package: str | None = None, tol: float = _CRYSTAL_TOL
    ) -> StockItem | None:
        """Match a generic crystal by frequency (tight tolerance), preferring same outline.

        BOMs often specify a crystal only weakly (e.g. "12MHz"), while stock holds a real MPN
        like ``X322512MOB4SI``. Matches on frequency within ``tol``; when several qualify,
        prefers one whose outline code matches, then the one with the most free stock.
        """
        if value_si is None:
            return None
        best, best_key = None, None
        for it in self.items:
            if it.comp_type != CompType.CRYSTAL or it.value_si is None:
                continue
            rel = abs(it.value_si - value_si) / value_si if value_si else 0.0
            if rel > tol:
                continue
            key = (1 if package and it.package == package else 0, it.free)
            if best is None or key > best_key:
                best, best_key = it, key
        return best

    def match_param(
        self, comp_type: CompType, value_si: float | None, package: str | None
    ) -> StockItem | None:
        if value_si is None:
            return None
        candidates = list(self.by_param.get((comp_type, package), []))
        if not candidates and package is None:
            for (ct, _pkg), lst in self.by_param.items():
                if ct == comp_type:
                    candidates.extend(lst)
        best, best_rel = None, _VALUE_TOL
        for it in candidates:
            if it.value_si is None:
                continue
            if value_si == 0:
                rel = 0.0 if it.value_si == 0 else 1e9
            else:
                rel = abs(it.value_si - value_si) / value_si
            if rel <= best_rel:
                best, best_rel = it, rel
        return best


def _parse_name(
    name: str, comp_type: CompType, description: str = ""
) -> tuple[float | None, str | None]:
    """Extract (value_si, package) from a miniMRP ItemName like '10uF/10V/20%/0402'.

    Crystals carry their frequency + outline in free text (ItemName/ItemDescription), e.g.
    '12MHz Crystal 12pF SMD3225-4P', so they are parsed from the combined text instead.
    """
    if comp_type == CompType.CRYSTAL:
        text = f"{name} {description}".strip()
        return extract_frequency(text), _crystal_package(text)
    tokens = [t.strip() for t in (name or "").split("/") if t.strip()]
    package = next((t for t in tokens if t in _IMPERIAL), None)
    value_si = None
    if comp_type in (CompType.RESISTOR, CompType.CAPACITOR, CompType.INDUCTOR) and tokens:
        value_si = parse_rkm_value(tokens[0], comp_type)
    return value_si, package


def _to_float(text: str | None) -> float:
    try:
        return float(text) if text not in (None, "") else 0.0
    except ValueError:
        return 0.0


def export_table(db_path: str | Path, table: str) -> list[dict[str, str]]:
    """Export one miniMRP table to a list of row dicts via ``mdb-export``."""
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"miniMRP database not found: {db_path}")
    if shutil.which("mdb-export") is None:
        raise RuntimeError(
            "mdb-export not found. Install mdbtools (e.g. `sudo apt install mdbtools`)."
        )
    out = subprocess.run(
        ["mdb-export", str(db_path), table],
        capture_output=True, text=True, check=True,
    ).stdout
    return list(csv.DictReader(io.StringIO(out)))


def read_stock_items(db_path: str | Path) -> list[StockItem]:
    items: list[StockItem] = []
    for row in export_table(db_path, "tblstockitems"):
        category = (row.get("Category") or "").upper()
        comp_type = _CATEGORY.get(category, CompType.OTHER)
        value_si, package = _parse_name(
            row.get("ItemName", ""), comp_type, row.get("ItemDescription", "")
        )
        items.append(
            StockItem(
                item_id=int(_to_float(row.get("ItemID"))),
                master_pno=(row.get("MasterPNo") or "").strip(),
                mfr_pno=(row.get("MfrPNo") or "").strip(),
                name=(row.get("ItemName") or "").strip(),
                description=(row.get("ItemDescription") or "").strip(),
                category=category,
                comp_type=comp_type,
                value_si=value_si,
                package=package,
                on_hand=_to_float(row.get("TotalQty")),
                allocated=_to_float(row.get("TotalAllocQty")),
                on_order=_to_float(row.get("TotalOnOrderQty")),
            )
        )
    return items


def load_stock_index(db_path: str | Path) -> StockIndex:
    return StockIndex.build(read_stock_items(db_path))
