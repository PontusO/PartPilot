import csv
from pathlib import Path

from digisearch.cart import purchasable_lines, review_lines, write_carts
from digisearch.models import BomLine, Candidate, ResolvedLine, LineKind, Status


def _line(refdes, supplier, dkpn, mpn, qty, status=Status.RESOLVED, packaging="Cut tape",
          reel_pn=None):
    cand = Candidate(supplier=supplier, mpn=mpn, dk_part_number=dkpn, reel_part_number=reel_pn)
    return ResolvedLine(
        line=BomLine(refdes=[refdes], qty=1), kind=LineKind.MPN, chosen=cand,
        status=status, packaging=packaging, purchase_qty=qty,
    )


def test_only_confident_lines_in_carts_review_separate():
    lines = [
        _line("U1", "Digi-Key", "DK1-ND", "MPN1", 100),               # resolved -> DK cart
        _line("U2", "Mouser", "81-MPN2", "MPN2", 50),                 # resolved -> Mouser cart
        _line("U4", "Digi-Key", "DK4-ND", "MPN4", 100, status=Status.REVIEW),  # review -> separate
        _line("U3", "Digi-Key", "DK3-ND", "MPN3", 0),                 # nothing to buy -> excluded
        ResolvedLine(line=BomLine(refdes=["C1"], qty=1), kind=LineKind.GENERIC_PASSIVE,
                     status=Status.IN_STOCK),                          # in stock -> excluded
    ]
    dk, mo = purchasable_lines(lines)
    assert [r.line.refdes_str for r in dk] == ["U1"]
    assert [r.line.refdes_str for r in mo] == ["U2"]
    assert [r.line.refdes_str for r in review_lines(lines)] == ["U4"]


def test_full_reel_uses_reel_part_number():
    line = _line("R1", "Digi-Key", "RES-CT-ND", "RMCF0402", 10000,
                 packaging="Full reel", reel_pn="RES-TR-ND")
    assert line.chosen.order_part_number(line.packaging) == "RES-TR-ND"


def test_write_carts_confident_and_review_files(tmp_path: Path):
    lines = [
        _line("U1", "Digi-Key", "DK1-ND", "MPN1", 100),                       # resolved
        _line("U2", "Mouser", "81-MPN2", "MPN2", 50),                         # resolved
        _line("U6", "Mouser", "81-APS6404", "APS6404L-3", 100, status=Status.REVIEW),
    ]
    carts = write_carts(lines, tmp_path / "out.xlsx")
    assert set(carts) == {"Digi-Key", "Mouser", "Review"}

    dk_rows = list(csv.DictReader(carts["Digi-Key"].open()))
    assert dk_rows[0]["Digi-Key Part Number"] == "DK1-ND"

    mo_rows = list(csv.DictReader(carts["Mouser"].open()))
    assert [r["Mouser Part Number"] for r in mo_rows] == ["81-MPN2"]  # U6 (review) excluded

    rev_rows = list(csv.DictReader(carts["Review"].open()))
    assert [r["Customer Reference"] for r in rev_rows] == ["U6"]
    assert rev_rows[0]["Supplier"] == "Mouser"


def test_no_buyable_lines_writes_nothing(tmp_path: Path):
    lines = [ResolvedLine(line=BomLine(refdes=["C1"], qty=1), kind=LineKind.GENERIC_PASSIVE,
                          status=Status.IN_STOCK)]
    assert write_carts(lines, tmp_path / "out.xlsx") == {}
