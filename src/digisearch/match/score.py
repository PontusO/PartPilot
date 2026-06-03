"""Score Digi-Key candidates against a parsed spec or a target MPN."""

from __future__ import annotations

import re

from rapidfuzz import fuzz

from ..config import MatchWeights
from ..models import Candidate, CompType, LineKind, PartSpec
from ..spec.units import (
    parse_capacitance,
    parse_frequency,
    parse_inductance,
    parse_resistance,
)

_ACTIVE = re.compile(r"active", re.IGNORECASE)
_DEAD = re.compile(r"obsolete|discontinued|end of life|not for new|last time", re.IGNORECASE)

_VALUE_PARAM_KEYS = {
    CompType.RESISTOR: ("resistance",),
    CompType.CAPACITOR: ("capacitance",),
    CompType.INDUCTOR: ("inductance",),
    CompType.CRYSTAL: ("frequency",),
}
_PARSERS = {
    CompType.RESISTOR: parse_resistance,
    CompType.CAPACITOR: parse_capacitance,
    CompType.INDUCTOR: parse_inductance,
    CompType.CRYSTAL: parse_frequency,
}


def _get_param(candidate: Candidate, *needles: str) -> str | None:
    for key, val in candidate.parameters.items():
        kl = key.lower()
        if any(n in kl for n in needles):
            return val
    return None


def _clean_numeric(text: str) -> str:
    return (
        text.lower()
        .replace("ohms", "")
        .replace("ohm", "")
        .replace("Ω", "")
        .replace("±", "")
        .strip()
    )


def _value_score(spec: PartSpec, candidate: Candidate) -> float:
    if spec.value_si is None:
        return 0.5  # nothing to compare against
    keys = _VALUE_PARAM_KEYS.get(spec.comp_type, ())
    raw = _get_param(candidate, *keys) if keys else None
    parser = _PARSERS.get(spec.comp_type)
    if not raw or parser is None:
        return 0.4  # candidate didn't expose the parameter
    cand_si = parser(_clean_numeric(raw))
    if cand_si is None:
        return 0.4
    if spec.value_si == 0:
        return 1.0 if cand_si == 0 else 0.0
    rel = abs(cand_si - spec.value_si) / spec.value_si
    if rel <= 0.02:
        return 1.0
    if rel <= 0.1:
        return 0.5
    return 0.0


def _package_score(spec: PartSpec, candidate: Candidate) -> float:
    if not spec.package_imperial:
        return 0.5
    pkg = _get_param(candidate, "package", "case")
    if not pkg:
        return 0.5
    return 1.0 if spec.package_imperial in pkg else 0.0


def _stock_score(candidate: Candidate) -> float:
    return 1.0 if candidate.quantity_available > 0 else 0.0


def _lifecycle_score(candidate: Candidate) -> float:
    status = candidate.lifecycle or ""
    if _DEAD.search(status):
        return 0.0
    if _ACTIVE.search(status):
        return 1.0
    return 0.5


def score_passive(spec: PartSpec, candidate: Candidate, w: MatchWeights) -> float:
    type_ok = 1.0 if spec.comp_type.value in (candidate.description or "").lower() else 0.5
    total = (
        w.value * _value_score(spec, candidate)
        + w.package * _package_score(spec, candidate)
        + w.in_stock * _stock_score(candidate)
        + w.lifecycle * _lifecycle_score(candidate)
        + w.type * type_ok
    )
    return total / (w.value + w.package + w.in_stock + w.lifecycle + w.type)


def _norm_mpn(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def score_mpn(target: str, candidate: Candidate, w: MatchWeights) -> float:
    tnorm = _norm_mpn(target)
    cnorm = _norm_mpn(candidate.mpn)
    if tnorm and cnorm and tnorm == cnorm:
        match = 1.0
    elif tnorm and cnorm and (tnorm in cnorm or cnorm in tnorm):
        match = 0.9
    else:
        match = fuzz.ratio(tnorm, cnorm) / 100.0 if cnorm else 0.0
    # For an MPN line, identity dominates; stock/lifecycle nudge confidence.
    return 0.75 * match + 0.15 * _stock_score(candidate) + 0.10 * _lifecycle_score(candidate)


def rank(
    kind: LineKind,
    candidates: list[Candidate],
    spec: PartSpec | None,
    target_mpn: str | None,
    weights: MatchWeights,
    build_qty: int = 1,
) -> list[tuple[float, Candidate]]:
    """Return candidates sorted best-first with their scores."""
    scored: list[tuple[float, Candidate]] = []
    for c in candidates:
        if kind == LineKind.MPN and target_mpn is not None:
            s = score_mpn(target_mpn, c, weights)
        elif spec is not None:
            s = score_passive(spec, c, weights)
        elif kind == LineKind.LOOKUP:
            # No parametric target; favour an in-stock, active part by category.
            s = 0.6 * _stock_score(c) + 0.4 * _lifecycle_score(c)
        else:
            s = 0.0
        scored.append((s, c))

    def sort_key(item: tuple[float, Candidate]):
        score, cand = item
        price = cand.price_at(build_qty)
        return (
            -round(score, 4),
            price if price is not None else float("inf"),
            -cand.quantity_available,
        )

    scored.sort(key=sort_key)
    return scored
