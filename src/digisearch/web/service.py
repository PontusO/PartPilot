"""Run a quote through the existing engine and collect its outputs.

This is the web layer's single entry point into the resolution pipeline. It mirrors the
CLI ``resolve`` flow (load -> resolve_bom -> write_report -> write_carts) but returns
structured results and the paths of the files it produced, so a request handler can
render a table and offer downloads. The engine itself is untouched and shared.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..bom.normalize import load_bom
from ..cart import write_carts
from ..config import (
    DigiKeyCredentials,
    MouserCredentials,
    Settings,
    load_column_mappings,
)
from ..models import ResolvedLine, Status
from ..pipeline import resolve_bom
from ..report.excel import write_report
from ..spec.lookup import load_lookup


@dataclass
class QuoteResult:
    resolved: list[ResolvedLine]
    report_path: Path
    cart_paths: dict[str, Path]  # {"Digi-Key"|"Mouser"|"Review": path}
    summary: dict[str, int]  # status value -> count
    build_qty: int
    currency: str
    total_cost: float
    stock_checked: bool
    mouser_enabled: bool
    warnings: list[str] = field(default_factory=list)


def _summary(resolved: list[ResolvedLine]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for line in resolved:
        counts[line.status.value] = counts.get(line.status.value, 0) + 1
    # Stable, status-enum order.
    return {s.value: counts[s.value] for s in Status if s.value in counts}


def run_quote(
    bom_path: str | Path,
    out_dir: str | Path,
    *,
    build_qty: int | None = None,
    currency: str | None = None,
    check_stock: bool = True,
    reel_threshold: float | None = None,
    sandbox: bool = False,
    settings_path: str | Path | None = None,
) -> QuoteResult:
    """Resolve ``bom_path`` and write the report + cart CSVs into ``out_dir``.

    Blocking and network-bound (Digi-Key/Mouser calls); callers in async routes must
    run this in a threadpool.
    """
    bom_path = Path(bom_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    settings = Settings.load(settings_path)
    mappings = load_column_mappings(None)
    lookup = load_lookup(None)
    lines = load_bom(bom_path, mappings)

    build_qty = build_qty if build_qty is not None else settings.build_qty
    currency = currency or settings.currency
    reel_threshold = settings.reel_threshold if reel_threshold is None else reel_threshold

    warnings: list[str] = []

    # Optional miniMRP stock pre-check (read-only), exactly as the CLI does.
    stock = None
    if check_stock and settings.minimrp_path:
        stock_path = Path(settings.minimrp_path)
        if stock_path.exists():
            try:
                from ..minimrp.reader import load_stock_index

                stock = load_stock_index(stock_path)
            except Exception as exc:  # missing mdbtools etc. shouldn't kill the quote
                warnings.append(f"Stock check skipped: {exc}")
        else:
            warnings.append(f"Stock check skipped: {stock_path} not found")

    creds = DigiKeyCredentials.from_env(sandbox=sandbox)
    if currency:
        creds.locale_currency = currency
    from ..digikey.client import DigiKeyClient

    client = DigiKeyClient(creds)

    mouser = None
    mo_creds = MouserCredentials.from_env()
    if mo_creds:
        from ..mouser.client import MouserClient

        mouser = MouserClient(mo_creds)

    resolved = resolve_bom(
        lines, client, settings, build_qty, lookup, stock, mouser, reel_threshold
    )

    report_path = out_dir / f"{bom_path.stem}-resolved.xlsx"
    write_report(resolved, report_path, build_qty=build_qty, currency=creds.locale_currency)
    cart_paths = write_carts(resolved, report_path)

    total_cost = sum(r.line_cost for r in resolved if r.line_cost)

    return QuoteResult(
        resolved=resolved,
        report_path=report_path,
        cart_paths=cart_paths,
        summary=_summary(resolved),
        build_qty=build_qty,
        currency=creds.locale_currency,
        total_cost=total_cost,
        stock_checked=stock is not None,
        mouser_enabled=mouser is not None,
        warnings=warnings,
    )
