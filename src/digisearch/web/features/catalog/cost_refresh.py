"""Re-query Digi-Key / Mouser for a part's supplier offers and refresh their captured cost tiers.

Only offers whose supplier is recognisably Digi-Key or Mouser and that carry a supplier part number
are refreshed; the distributor is looked up by that part number and its price breaks overwrite the
offer's cost tiers. Every offer is handled independently — a network error on one is reported, not
fatal. The part's flat unit cost is intentionally left untouched (this only refreshes the ladders).
"""

from __future__ import annotations

import re

from digisearch.config import DigiKeyCredentials, MouserCredentials

from ...core.db import Database
from . import repo


def _distributor_of(name: str | None) -> str | None:
    """Which distributor a supplier name maps to (matching ``distributor_url``'s normalisation)."""
    norm = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    if "digikey" in norm:
        return "digikey"
    if "mouser" in norm:
        return "mouser"
    return None


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _build_clients() -> tuple[object | None, object | None]:
    """(digikey_client, mouser_client) — each None when that distributor isn't configured."""
    dk = None
    try:
        from digisearch.digikey.client import DigiKeyClient
        dk = DigiKeyClient(DigiKeyCredentials.from_env())
    except Exception:            # missing/invalid Digi-Key creds -> treat as not configured
        dk = None
    mo = None
    mo_creds = MouserCredentials.from_env()
    if mo_creds:
        from digisearch.mouser.client import MouserClient
        mo = MouserClient(mo_creds)
    return dk, mo


def build_clients() -> tuple[object | None, object | None]:
    """Public: build the distributor clients once, to reuse across many offers (e.g. per PO line)."""
    return _build_clients()


def _match(candidates: list, supplier_pno: str):
    """Pick the candidate matching the offer's supplier P/N (distributor P/N, then MPN), else the
    first result."""
    n = _norm(supplier_pno)
    for c in candidates:
        if _norm(c.dk_part_number) == n:
            return c
    for c in candidates:
        if _norm(c.mpn) == n:
            return c
    return candidates[0] if candidates else None


def fetch_offer_breaks(clients, supplier_name: str | None, supplier_pno: str | None):
    """Query the distributor for one supplier offer and return ``(cut_tiers, reel_tiers)`` (lists of
    ``{"break_qty", "unit_price"}``), or ``None`` when it can't be priced live — not a Digi-Key/Mouser
    supplier, that distributor isn't configured, no supplier P/N, the lookup fails, or no breaks come
    back. ``clients`` is the ``(dk, mo)`` tuple from :func:`build_clients`."""
    dk, mo = clients
    dist = _distributor_of(supplier_name)
    pno = (supplier_pno or "").strip()
    if dist is None or not pno:
        return None
    client = dk if dist == "digikey" else mo
    if client is None:
        return None
    try:
        candidates = client.keyword_search(pno, limit=5)
    except Exception:                          # network / auth / rate-limit
        return None
    cand = _match(candidates, pno)
    if cand is None or (not cand.price_breaks and not cand.reel_price_breaks):
        return None
    cut = [{"break_qty": bq, "unit_price": p} for bq, p in cand.price_breaks]
    reel = [{"break_qty": bq, "unit_price": p} for bq, p in cand.reel_price_breaks]
    return cut, reel


def refresh_cost_tiers(db: Database, part_id: int) -> dict:
    """Refresh cost tiers for every Digi-Key/Mouser offer on the part. Returns
    ``{"updated": [...], "skipped": [...], "errors": [...]}`` of human-readable lines."""
    part = repo.get_part(db, part_id)
    if part is None:
        return {"updated": [], "skipped": [], "errors": ["Part not found."]}

    dk, mo = _build_clients()
    updated: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []

    for s in part["suppliers"]:
        name = s.get("supplier_name") or "?"
        pno = (s.get("supplier_pno") or "").strip()
        label = f"{name} {pno}".strip()
        dist = _distributor_of(name)
        if dist is None:
            skipped.append(f"{label}: not Digi-Key or Mouser")
            continue
        if not pno:
            skipped.append(f"{name}: no supplier part number to look up")
            continue
        client = dk if dist == "digikey" else mo
        if client is None:
            errors.append(f"{label}: {dist.title()} is not configured (.env credentials)")
            continue
        try:
            candidates = client.keyword_search(pno, limit=5)
        except Exception as exc:               # network / auth / rate-limit
            errors.append(f"{label}: lookup failed — {exc}")
            continue
        cand = _match(candidates, pno)
        if cand is None or (not cand.price_breaks and not cand.reel_price_breaks):
            skipped.append(f"{label}: no price breaks returned")
            continue
        cut = [{"break_qty": bq, "unit_price": p} for bq, p in cand.price_breaks]
        reel = [{"break_qty": bq, "unit_price": p} for bq, p in cand.reel_price_breaks]
        repo.replace_cost_tiers(db, s["id"], cut, reel)
        bits = []
        if cut:
            bits.append(f"{len(cut)} cut")
        if reel:
            bits.append(f"{len(reel)} reel")
        updated.append(f"{label}: {', '.join(bits)} break(s)")

    return {"updated": updated, "skipped": skipped, "errors": errors}
