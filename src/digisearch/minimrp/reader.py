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
from ..spec.units import parse_rkm_value

_CATEGORY = {
    "RESISTOR": CompType.RESISTOR,
    "CAPACITOR": CompType.CAPACITOR,
    "INDUCTOR": CompType.INDUCTOR,
    "CRYSTAL": CompType.CRYSTAL,
}
_IMPERIAL = {"0201", "0402", "0603", "0805", "1206", "1210", "1812", "2010", "2512", "0806", "1008"}
_VALUE_TOL = 0.02  # passives within 2% are the same nominal value


def _norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


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


def _parse_name(name: str, comp_type: CompType) -> tuple[float | None, str | None]:
    """Extract (value_si, package) from a miniMRP ItemName like '10uF/10V/20%/0402'."""
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


def read_stock_items(db_path: str | Path) -> list[StockItem]:
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"miniMRP database not found: {db_path}")
    if shutil.which("mdb-export") is None:
        raise RuntimeError(
            "mdb-export not found. Install mdbtools (e.g. `sudo apt install mdbtools`)."
        )
    out = subprocess.run(
        ["mdb-export", str(db_path), "tblstockitems"],
        capture_output=True, text=True, check=True,
    ).stdout
    items: list[StockItem] = []
    for row in csv.DictReader(io.StringIO(out)):
        category = (row.get("Category") or "").upper()
        comp_type = _CATEGORY.get(category, CompType.OTHER)
        value_si, package = _parse_name(row.get("ItemName", ""), comp_type)
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
