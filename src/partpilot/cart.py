"""Write distributor BOM-upload CSVs for the lines that need purchasing.

The CSVs are ready to upload to each distributor's list/BOM importer (which becomes
a cart): Digi-Key's "Upload a List" and Mouser's "BOM Import". Both let you map
columns on import, so the exact header names are not critical.
"""

from __future__ import annotations

import csv
from pathlib import Path

from .models import ResolvedLine, Status


def _needs_buying(r: ResolvedLine) -> bool:
    return r.chosen is not None and (r.purchase_qty or 0) > 0


def purchasable_lines(resolved: list[ResolvedLine]) -> tuple[list[ResolvedLine], list[ResolvedLine]]:
    """Confidently-resolved lines that need buying, split into (digikey, mouser)."""
    dk, mo = [], []
    for r in resolved:
        if _needs_buying(r) and r.status == Status.RESOLVED:
            (mo if r.chosen.supplier == "Mouser" else dk).append(r)
    return dk, mo


def review_lines(resolved: list[ResolvedLine]) -> list[ResolvedLine]:
    """Lines that need buying but are flagged for review (kept out of the auto carts)."""
    return [r for r in resolved if _needs_buying(r) and r.status == Status.REVIEW]


def _write_csv(path: Path, header: list[str], rows: list[list]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)
    return path


def _digikey_csv(lines: list[ResolvedLine], path: Path) -> Path:
    header = ["Quantity", "Digi-Key Part Number", "Manufacturer Part Number", "Customer Reference"]
    rows = [
        [
            r.purchase_qty,
            r.chosen.order_part_number(r.packaging),
            r.chosen.mpn,
            r.line.refdes_str,
        ]
        for r in lines
    ]
    return _write_csv(path, header, rows)


def _mouser_csv(lines: list[ResolvedLine], path: Path) -> Path:
    header = ["Mouser Part Number", "Quantity", "Manufacturer Part Number", "Customer Reference"]
    rows = [
        [r.chosen.dk_part_number, r.purchase_qty, r.chosen.mpn, r.line.refdes_str]
        for r in lines
    ]
    return _write_csv(path, header, rows)


def _review_csv(lines: list[ResolvedLine], path: Path) -> Path:
    header = [
        "Customer Reference", "Supplier", "Quantity", "Packaging",
        "Distributor Part Number", "Manufacturer Part Number", "Original value",
        "Confidence", "Flag",
    ]
    rows = [
        [
            r.line.refdes_str,
            r.chosen.supplier,
            r.purchase_qty,
            r.packaging,
            r.chosen.order_part_number(r.packaging),
            r.chosen.mpn,
            r.line.value or r.line.device,
            round(r.confidence, 3),
            r.flag_reason,
        ]
        for r in lines
    ]
    return _write_csv(path, header, rows)


def write_carts(resolved: list[ResolvedLine], base_path: str | Path) -> dict[str, Path]:
    """Write cart CSVs (confident lines only) + a needs-review CSV next to ``base_path``.

    Returns {"Digi-Key"|"Mouser"|"Review": path} for whichever were written.
    """
    base = Path(base_path)
    dk, mo = purchasable_lines(resolved)
    review = review_lines(resolved)
    written: dict[str, Path] = {}
    if dk:
        written["Digi-Key"] = _digikey_csv(dk, base.with_name(f"{base.stem}-digikey-cart.csv"))
    if mo:
        written["Mouser"] = _mouser_csv(mo, base.with_name(f"{base.stem}-mouser-cart.csv"))
    if review:
        written["Review"] = _review_csv(review, base.with_name(f"{base.stem}-needs-review.csv"))
    return written
