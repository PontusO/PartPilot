"""The compound internal part-number format.

Invector's internal numbers are ``PREFIX-NNNNN-S``: a 2-digit prefix (category / customer), a
5-digit running number that is the shared *identity* of a product family, and an un-padded suffix
that distinguishes variants/revisions within that prefix + running number. E.g. ``98-00002-4``.

This mirrors the Excel formula ``CONCAT(TEXT(A,"00"), TEXT(B,"-00000-"), TEXT(C,"0"))`` that the
register grew up on, and sits alongside :func:`digisearch.web.core.refs.ref_no` (the 2-part
``PREFIX-NNNNN`` sibling used for PO/WO/DN references).
"""

from __future__ import annotations

RUNNING_WIDTH = 5
PREFIX_WIDTH = 2

# Separator inserted between a product's base name and a template line's label, e.g.
# "MiThings GW motherboard" + "PCB" -> "MiThings GW motherboard – PCB". En dash, spaced.
DESC_SEP = " – "


def article_code(prefix: str, running_no: int, suffix: int) -> str:
    return f"{prefix}-{running_no:0{RUNNING_WIDTH}d}-{suffix}"


def compose_description(product: str | None, label: str | None) -> str | None:
    """Build a line's description from the base product name and a template label.

    ``product`` alone when the label is blank (e.g. the assembly line); otherwise
    ``"<product>{DESC_SEP}<label>"``. Returns ``None`` if both are empty.
    """
    product = (product or "").strip()
    label = (label or "").strip()
    if product and label:
        return f"{product}{DESC_SEP}{label}"
    return product or label or None


def normalize_prefix(prefix: str | int | None) -> str | None:
    """A prefix is stored zero-padded to 2 chars ('1' -> '01'); blank/None means an unassigned
    (reserved) running number."""
    if prefix is None:
        return None
    text = str(prefix).strip()
    if not text:
        return None
    return text.zfill(PREFIX_WIDTH)
