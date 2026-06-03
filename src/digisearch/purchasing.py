"""Decide full-reel vs cut-tape sourcing for a purchased line."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .models import Candidate


@dataclass
class PurchaseDecision:
    packaging: str  # "Full reel" / "Cut tape" / "—"
    qty: int  # quantity actually ordered
    unit_price: float | None
    line_cost: float


def decide_packaging(
    candidate: Candidate | None, order_qty: int, reel_threshold: float
) -> PurchaseDecision:
    """Full reel when the whole reel(s) cost under ``reel_threshold``, else cut tape.

    Multi-reel lines round up to whole reels. ``reel_threshold`` of 0 disables reels
    (always cut tape). Quantities/prices are in the resolver's locale currency.
    """
    if candidate is None or order_qty <= 0:
        return PurchaseDecision("—", 0, None, 0.0)

    cut_unit = candidate.price_at(order_qty)
    cut_cost = (cut_unit or 0.0) * order_qty

    if reel_threshold and candidate.reel_qty and candidate.reel_price_breaks:
        reels = math.ceil(order_qty / candidate.reel_qty)
        reel_total = reels * candidate.reel_qty
        reel_unit = candidate.reel_price_at(reel_total)
        if reel_unit is not None:
            reel_cost = reel_unit * reel_total
            if reel_cost < reel_threshold:
                return PurchaseDecision("Full reel", reel_total, reel_unit, reel_cost)

    return PurchaseDecision("Cut tape", order_qty, cut_unit, cut_cost)
