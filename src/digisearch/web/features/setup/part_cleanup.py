"""Recover a manufacturer P/N for a part whose ``part_no`` is really a supplier order code.

Two strategies, cheapest first:

1. The Digi-Key ``<MPN>-ND`` convention — when stripping ``-ND`` leaves something with a letter
   in it (an MPN, not a bare catalogue number like ``399-16094-6``), that's the manufacturer P/N
   with no API call.
2. Re-query the distributor the part is actually sourced from (Digi-Key preferred, then Mouser)
   and take the manufacturer P/N from the match — but only if the distributor's *own* number on
   the returned product equals the one we searched, so a fuzzy mismatch can't be promoted.

Used by the Setup → Part-number cleanup tool. The clients are built once per request and reused
across parts to avoid re-authenticating per lookup.
"""
from __future__ import annotations

from dataclasses import dataclass

from digisearch.config import DigiKeyCredentials, MouserCredentials


@dataclass
class Recovery:
    mpn: str | None = None
    manufacturer: str | None = None
    source: str | None = None      # 'rule', 'Digi-Key', 'Mouser'
    note: str | None = None        # why nothing was found, for the UI


def _norm(s: str | None) -> str:
    return (s or "").replace(" ", "").replace("-", "").lower()


def build_clients():
    """(digikey_client, mouser_client_or_None). Raises if Digi-Key creds are absent."""
    from digisearch.digikey.client import DigiKeyClient

    dk = DigiKeyClient(DigiKeyCredentials.from_env(sandbox=False))
    mouser = None
    mo_creds = MouserCredentials.from_env()
    if mo_creds:
        from digisearch.mouser.client import MouserClient

        mouser = MouserClient(mo_creds)
    return dk, mouser


def _verified_match(client, query: str):
    """First candidate whose distributor number equals ``query`` and that carries an MPN."""
    for c in client.keyword_search(query, limit=3) or []:
        if c and c.mpn and _norm(c.dk_part_number) == _norm(query):
            return c
    return None


def recover(part_no: str, suppliers: list[dict], dk, mouser) -> Recovery:
    """Best-effort manufacturer P/N for one part. ``suppliers`` is a list of
    ``{supplier, supplier_pno}`` dicts."""
    # 1) '-ND' strip when the remainder looks like an MPN (has a letter).
    if part_no.endswith("-ND"):
        base = part_no[:-3]
        if any(ch.isalpha() for ch in base):
            return Recovery(mpn=base, source="rule", note="stripped Digi-Key '-ND' suffix")

    # Build the queries to try, Digi-Key first, then Mouser.
    dk_queries, mo_queries = [], []
    if part_no.endswith("-ND"):
        dk_queries.append(part_no)
    for s in suppliers:
        name = (s.get("supplier") or "").lower()
        pno = s.get("supplier_pno")
        if not pno:
            continue
        if "digi" in name:
            dk_queries.append(pno)
        elif "mouser" in name:
            mo_queries.append(pno)

    for q in dict.fromkeys(dk_queries):          # de-dupe, keep order
        c = _verified_match(dk, q)
        if c:
            return Recovery(mpn=c.mpn, manufacturer=c.manufacturer, source="Digi-Key",
                            note=f"matched Digi-Key {q}")
    if mouser:
        for q in dict.fromkeys(mo_queries):
            c = _verified_match(mouser, q)
            if c:
                return Recovery(mpn=c.mpn, manufacturer=c.manufacturer, source="Mouser",
                                note=f"matched Mouser {q}")

    sourced = ", ".join(sorted({(s.get("supplier") or "?") for s in suppliers})) or "—"
    return Recovery(note=f"no Digi-Key/Mouser match (sourced from {sourced})")
