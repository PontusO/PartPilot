"""Shared convention for system-generated reference numbers.

All auto-generated document references use ``PREFIX-NNNNN`` with the number zero-padded to at
least 5 digits (PO-00009, WO-00001, DN-00001, GRN-00001). Use this everywhere a reference is
auto-assigned so the format stays consistent across documents.
"""

from __future__ import annotations

REF_WIDTH = 5


def ref_no(prefix: str, n: int, width: int = REF_WIDTH) -> str:
    return f"{prefix}-{n:0{width}d}"
