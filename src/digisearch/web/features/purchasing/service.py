"""Resolve a BOM for purchasing and collect the outputs.

The purchasing feature's single entry point into the resolution pipeline. It mirrors the
CLI ``resolve`` flow (load -> resolve_bom -> write_report -> write_carts) but returns
structured results and the paths of the files it produced. The engine is untouched and
shared with the CLI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from digisearch.bom.normalize import load_bom
from digisearch.cart import write_carts
from digisearch.config import (
    DigiKeyCredentials,
    MouserCredentials,
    Settings,
    load_column_mappings,
)
from digisearch.models import ResolvedLine, Status
from digisearch.pipeline import resolve_bom
from digisearch.report.excel import write_report
from digisearch.spec.lookup import load_lookup


@dataclass
class PurchaseResult:
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
    return {s.value: counts[s.value] for s in Status if s.value in counts}


@dataclass
class ResolvedRun:
    """The shared resolution result — a BOM resolved into lines, no output files yet."""

    resolved: list[ResolvedLine]
    build_qty: int
    currency: str
    stock_checked: bool
    mouser_enabled: bool
    warnings: list[str] = field(default_factory=list)


def resolve_bom_file(
    bom_path: str | Path,
    *,
    build_qty: int | None = None,
    currency: str | None = None,
    check_stock: bool = True,
    reel_threshold: float | None = None,
    sandbox: bool = False,
    settings_path: str | Path | None = None,
) -> ResolvedRun:
    """Load and resolve a BOM file against stock + Digi-Key/Mouser.

    This is the reusable heart of the purchasing tool: both the purchasing flow and the
    assembly BOM import call it, so resolution changes propagate to both. Blocking and
    network-bound — run it in a threadpool from async routes.
    """
    bom_path = Path(bom_path)
    settings = Settings.load(settings_path)
    mappings = load_column_mappings(None)
    lookup = load_lookup(None)
    lines = load_bom(bom_path, mappings)

    build_qty = build_qty if build_qty is not None else settings.build_qty
    currency = currency or settings.currency
    reel_threshold = settings.reel_threshold if reel_threshold is None else reel_threshold

    warnings: list[str] = []
    stock = None
    if check_stock and settings.minimrp_path:
        stock_path = Path(settings.minimrp_path)
        if stock_path.exists():
            try:
                from digisearch.minimrp.reader import load_stock_index

                stock = load_stock_index(stock_path)
            except Exception as exc:  # missing mdbtools etc. shouldn't kill the run
                warnings.append(f"Stock check skipped: {exc}")
        else:
            warnings.append(f"Stock check skipped: {stock_path} not found")

    creds = DigiKeyCredentials.from_env(sandbox=sandbox)
    if currency:
        creds.locale_currency = currency
    from digisearch.digikey.client import DigiKeyClient

    client = DigiKeyClient(creds)

    mouser = None
    mo_creds = MouserCredentials.from_env()
    if mo_creds:
        from digisearch.mouser.client import MouserClient

        mouser = MouserClient(mo_creds)

    resolved = resolve_bom(
        lines, client, settings, build_qty, lookup, stock, mouser, reel_threshold
    )
    return ResolvedRun(
        resolved=resolved, build_qty=build_qty, currency=creds.locale_currency,
        stock_checked=stock is not None, mouser_enabled=mouser is not None, warnings=warnings,
    )


def run_purchase(
    bom_path: str | Path,
    out_dir: str | Path,
    *,
    build_qty: int | None = None,
    currency: str | None = None,
    check_stock: bool = True,
    reel_threshold: float | None = None,
    sandbox: bool = False,
    settings_path: str | Path | None = None,
) -> PurchaseResult:
    """Resolve ``bom_path`` (via :func:`resolve_bom_file`) and write the report + cart CSVs."""
    bom_path = Path(bom_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    run = resolve_bom_file(
        bom_path, build_qty=build_qty, currency=currency, check_stock=check_stock,
        reel_threshold=reel_threshold, sandbox=sandbox, settings_path=settings_path,
    )

    report_path = out_dir / f"{bom_path.stem}-resolved.xlsx"
    write_report(run.resolved, report_path, build_qty=run.build_qty, currency=run.currency)
    cart_paths = write_carts(run.resolved, report_path)
    total_cost = sum(r.line_cost for r in run.resolved if r.line_cost)

    return PurchaseResult(
        resolved=run.resolved,
        report_path=report_path,
        cart_paths=cart_paths,
        summary=_summary(run.resolved),
        build_qty=run.build_qty,
        currency=run.currency,
        total_cost=total_cost,
        stock_checked=run.stock_checked,
        mouser_enabled=run.mouser_enabled,
        warnings=run.warnings,
    )
