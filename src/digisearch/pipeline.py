"""Resolve a list of BOM lines into ResolvedLine records via Digi-Key."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from .config import Settings
from .models import BomLine, Candidate, CompType, LineKind, PartSpec, ResolvedLine, Status
from .match.score import rank
from .minimrp.reader import StockIndex, StockItem
from .purchasing import decide_packaging
from .spec.classify import classify, looks_like_mpn
from .spec.lookup import LookupRule, match_lookup, render
from .spec.parse_spec import build_query, build_spec, mpn_query, relaxed_mpn_queries

_PASSIVE = (CompType.RESISTOR, CompType.CAPACITOR, CompType.INDUCTOR)


class Searcher(Protocol):
    def keyword_search(self, keywords: str, limit: int = 5) -> list[Candidate]: ...


_NO_SEARCH = {
    LineKind.DNP: Status.DNP,
    LineKind.NON_ORDERABLE: Status.NON_ORDERABLE,
}

# Catalogue noise that is never a valid single BOM line item.
_NON_PART = re.compile(r"\b(kit|assortment|sampler|samples?)\b", re.IGNORECASE)


def _drop_non_parts(candidates: list[Candidate]) -> list[Candidate]:
    filtered = [c for c in candidates if not (c.description and _NON_PART.search(c.description))]
    return filtered or candidates  # never empty out a result set entirely


@dataclass
class LinePlan:
    """How a line will be searched, before any API call."""

    kind: LineKind
    query: str | None
    spec: PartSpec | None
    target_mpn: str | None


def plan_line(line: BomLine, settings: Settings, lookup: list[LookupRule] | None = None) -> LinePlan:
    """Classify a line and decide its search, applying the device-lookup table."""
    kind = classify(line, settings)

    if kind not in _NO_SEARCH:
        hit = match_lookup(line, lookup or [])
        if hit is not None:
            if hit.mpn:
                target = render(hit.mpn, line)
                return LinePlan(LineKind.MPN, target, None, target)
            return LinePlan(LineKind.LOOKUP, render(hit.query or "", line), None, None)

    if kind in (LineKind.GENERIC_PASSIVE, LineKind.CRYSTAL):
        spec = build_spec(line, settings, kind)
        return LinePlan(kind, build_query(spec), spec, None)
    if kind == LineKind.MPN:
        query = mpn_query(line)
        return LinePlan(kind, query, None, query)
    return LinePlan(kind, None, None, None)  # DNP / NON_ORDERABLE


def _needs_manual(plan: LinePlan, line: BomLine) -> bool:
    """A line the BOM under-specifies so badly the tool can't pick a part (needs a human).

    Examples: a passive/crystal with no parseable value (C38, L1), or a line whose only
    identifier is a generic EAGLE device name equal to its package (SV1 = MA13-2 / MA13-2).
    """
    if not plan.query:
        return True
    if plan.kind in (LineKind.GENERIC_PASSIVE, LineKind.CRYSTAL):
        return plan.spec is None or plan.spec.value_si is None
    if plan.kind == LineKind.MPN and not (line.value or "").strip():
        dev = (line.device or "").strip().lower()
        pkg = (line.package or "").strip().lower()
        return (not dev) or dev == pkg or not looks_like_mpn(line.device)
    return False


def _match_stock(plan: LinePlan, line: BomLine, stock: StockIndex) -> tuple[StockItem | None, str]:
    """Find a stock item for this line; also report *how* it matched.

    Returns (item, how) where how is "param" (passive value+package), "crystal"
    (frequency), "exact" (MPN), "stem" (fuller MPN), or "none".
    """
    spec = plan.spec
    if spec is not None and spec.comp_type in _PASSIVE:
        return stock.match_param(spec.comp_type, spec.value_si, spec.package_imperial), "param"
    if spec is not None and spec.comp_type == CompType.CRYSTAL and spec.value_si is not None:
        crystal = stock.match_crystal(spec.value_si, spec.package_code)
        if crystal is not None:
            return crystal, "crystal"
    hit = stock.match_mpn(plan.target_mpn, line.value, line.device)
    if hit is not None:
        return hit, "exact"
    # Fall back to a fuller stocked MPN (e.g. BOM "MBR120" -> stock "MBR120LSF").
    hit = stock.match_mpn_prefix(plan.target_mpn, line.value, line.device)
    return (hit, "stem") if hit is not None else (None, "none")


def _weak_stock_note(how: str, plan: LinePlan, line: BomLine, match: StockItem) -> str:
    """A 'verify' note for stock matches looser than exact (stem MPN / generic crystal)."""
    if how == "stem":
        return f"MPN stem '{plan.target_mpn or line.value}' → {match.label}; verify"
    if how == "crystal":
        freq = plan.spec.value_display if plan.spec else None
        return f"crystal matched by {freq or 'frequency'} → {match.label}; verify"
    return ""  # "exact"/"param" matches are trusted — no extra note


def _search_supplier(
    searcher: Searcher, plan: LinePlan, settings: Settings, build_qty: int
) -> tuple[list[tuple[float, "object"]], bool, str | None]:
    """Search one supplier; return (ranked candidates, relaxed_flag, error_or_None)."""
    try:
        candidates = searcher.keyword_search(plan.query, limit=settings.candidates_per_line)
        relaxed = False
        if not candidates and plan.kind == LineKind.MPN:
            for fb in relaxed_mpn_queries(plan.query):
                candidates = searcher.keyword_search(fb, limit=settings.candidates_per_line)
                if candidates:
                    relaxed = True
                    break
    except Exception as exc:  # one bad supplier/line shouldn't abort the whole BOM
        return [], False, str(exc)
    if not candidates:
        return [], False, None
    candidates = _drop_non_parts(candidates)
    ranked = rank(plan.kind, candidates, plan.spec, plan.target_mpn, settings.weights, build_qty)
    return ranked, relaxed, None


def resolve_line(
    line: BomLine,
    client: Searcher,
    settings: Settings,
    build_qty: int = 1,
    lookup: list[LookupRule] | None = None,
    stock: StockIndex | None = None,
    mouser: Searcher | None = None,
    reel_threshold: float = 0.0,
) -> ResolvedLine:
    plan = plan_line(line, settings, lookup)

    if plan.kind in _NO_SEARCH:
        return ResolvedLine(line=line, kind=plan.kind, status=_NO_SEARCH[plan.kind])

    result = ResolvedLine(line=line, kind=plan.kind, spec=plan.spec, query=plan.query)
    if _needs_manual(plan, line):
        result.status = Status.MANUAL
        result.flag_reason = "no value / part number to resolve from — needs manual selection"
        return result

    # miniMRP stock pre-check: skip Digi-Key entirely when fully covered by stock.
    required = line.qty * build_qty
    stock_note = ""  # extra "verify" note for weak (stem MPN / generic crystal) stock matches
    if stock is not None:
        match, how = _match_stock(plan, line, stock)
        if match is not None:
            stock_note = _weak_stock_note(how, plan, line, match)
            result.stock_on_hand = match.on_hand
            result.stock_free = match.free
            result.stock_match = match.label
            result.need_to_buy = max(0, required - int(match.free))
            if result.need_to_buy == 0:
                result.status = Status.IN_STOCK
                note = f"in stock: {match.label} ({int(match.free)} free ≥ {required} needed)"
                if stock_note:
                    note += f" [{stock_note}]"
                if plan.spec and plan.spec.value_warning:
                    note += f"; {plan.spec.value_warning}"
                result.flag_reason = note
                result.packaging, result.purchase_qty, result.line_cost = "—", 0, 0.0
                return result
        else:
            result.need_to_buy = required  # nothing in stock -> buy the full quantity

    value_warning = plan.spec.value_warning if plan.spec else None

    # Digi-Key is the preferred supplier; consult Mouser when DK is weak — a low-confidence
    # match, no match, OR a best match that is out of stock. Mouser wins if it out-scores DK,
    # or if it is actually in stock when DK's pick is not (ties otherwise go to Digi-Key).
    ranked, relaxed, dk_err = _search_supplier(client, plan, settings, build_qty)
    mo_err = None
    dk_out_of_stock = bool(ranked) and ranked[0][1].quantity_available <= 0
    dk_weak = (not ranked) or ranked[0][0] < settings.confidence_threshold or dk_out_of_stock
    if mouser is not None and dk_weak:
        mo_ranked, mo_relaxed, mo_err = _search_supplier(mouser, plan, settings, build_qty)
        if mo_ranked:
            mo_in_stock = mo_ranked[0][1].quantity_available > 0
            if (not ranked or mo_ranked[0][0] > ranked[0][0]
                    or (dk_out_of_stock and mo_in_stock)):
                ranked, relaxed = mo_ranked, mo_relaxed

    if not ranked:
        if dk_err and (mouser is None or mo_err):
            result.status = Status.ERROR
            result.flag_reason = f"search error: {dk_err}"
        else:
            result.status = Status.NOT_FOUND
            result.flag_reason = value_warning or "no Digi-Key or Mouser matches"
        return result

    best_score, best = ranked[0]
    result.chosen = best
    result.confidence = round(best_score, 3)
    result.alternates = [c for _, c in ranked[1 : 1 + settings.alternates_kept]]

    reasons: list[str] = []
    if relaxed:
        # Found only via a trimmed MPN -> nearest match, not the exact part.
        result.status = Status.REVIEW
        reasons.append(f"exact MPN '{plan.query}' not found — nearest match, verify")
    elif plan.kind == LineKind.LOOKUP:
        # Generic lookup query -> always worth a human glance.
        result.status = Status.REVIEW
        reasons.append("generic lookup match — verify part")
    elif best_score < settings.confidence_threshold:
        result.status = Status.REVIEW
        reasons.append(f"low confidence ({best_score:.0%})")
    else:
        result.status = Status.RESOLVED
    if best.supplier and best.supplier != "Digi-Key":
        reasons.append(f"sourced from {best.supplier}")
    if value_warning:
        reasons.append(value_warning)
        result.status = Status.REVIEW  # value itself is suspect -> always review
    if plan.spec and plan.spec.assumed:
        reasons.append("assumed " + ", ".join(sorted(set(plan.spec.assumed))))
        if result.status == Status.RESOLVED:
            result.status = Status.REVIEW  # under-specified -> always worth a glance
    if best.quantity_available <= 0:
        reasons.append(f"out of stock at {best.supplier or 'supplier'}")
        if result.status == Status.RESOLVED:
            result.status = Status.REVIEW
    if result.stock_match and result.need_to_buy and result.need_to_buy > 0:
        msg = f"partial stock: {int(result.stock_free)} free, buy {result.need_to_buy}"
        if stock_note:
            msg += f" ({stock_note})"
        reasons.append(msg)

    decision = decide_packaging(result.chosen, result.order_qty(required), reel_threshold)
    result.packaging = decision.packaging
    result.purchase_qty = decision.qty
    result.purchase_unit_price = decision.unit_price
    result.line_cost = decision.line_cost
    if decision.packaging == "Full reel" and decision.qty > result.order_qty(required):
        reasons.append(f"full reel ({decision.qty}); extra to stock")

    result.flag_reason = "; ".join(reasons) or None
    return result


def resolve_bom(
    lines: list[BomLine],
    client: Searcher,
    settings: Settings,
    build_qty: int = 1,
    lookup: list[LookupRule] | None = None,
    stock: StockIndex | None = None,
    mouser: Searcher | None = None,
    reel_threshold: float = 0.0,
) -> list[ResolvedLine]:
    return [
        resolve_line(line, client, settings, build_qty, lookup, stock, mouser, reel_threshold)
        for line in lines
    ]
