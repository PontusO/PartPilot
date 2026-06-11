"""DigiSearch command-line interface."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from .bom.normalize import load_bom
from .config import DigiKeyCredentials, MouserCredentials, Settings, load_column_mappings
from .models import Status
from .pipeline import plan_line, resolve_bom
from .report.excel import write_report
from .spec.lookup import load_lookup

app = typer.Typer(add_completion=False, help="Resolve simplified BOMs to real Digi-Key parts.")
console = Console()


def _load_creds(sandbox: bool) -> DigiKeyCredentials:
    try:
        return DigiKeyCredentials.from_env(sandbox=sandbox)
    except RuntimeError as exc:
        console.print(f"[red]Error:[/] {exc}")
        raise typer.Exit(code=1)


@app.command()
def auth_test(
    sandbox: bool = typer.Option(False, "--sandbox", help="Use the Digi-Key sandbox (mock data) instead of production."),
):
    """Fetch an access token to verify credentials and headers."""
    from .digikey.auth import TokenManager

    creds = _load_creds(sandbox)
    console.print(f"Environment: [bold]{'sandbox' if creds.sandbox else 'production'}[/] "
                  f"({creds.base_url})")
    token = TokenManager(creds).get_token()
    console.print(f"[green]OK[/] — received access token ({len(token)} chars). "
                  "Credentials are valid.")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind address. Use 0.0.0.0 to allow LAN access."),
    port: int = typer.Option(8000, help="Port to listen on."),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on code changes (dev only)."),
):
    """Run the PartPilot web app (upload a BOM, resolve it for purchasing in the browser)."""
    import uvicorn

    console.print(
        f"Starting PartPilot on [bold]http://{host}:{port}[/] "
        f"{'(LAN-accessible)' if host == '0.0.0.0' else '(local only — use --host 0.0.0.0 for LAN)'}"
    )
    uvicorn.run("digisearch.web.app:create_app", host=host, port=port, factory=True, reload=reload)


@app.command(name="import-catalog")
def import_catalog(
    minimrp: Optional[Path] = typer.Option(
        None, "--from", help="miniMRP database (Data/mrp5data). Defaults to settings minimrp_path."
    ),
):
    """Import the miniMRP catalog (parts, suppliers, stock) into PartPilot's database."""
    settings = Settings.load(None)
    src = minimrp or (Path(settings.minimrp_path) if settings.minimrp_path else None)
    if not src or not Path(src).exists():
        console.print(
            "[red]Error:[/] miniMRP database not found. Pass --from or set minimrp_path in settings."
        )
        raise typer.Exit(1)

    from .web.app import FEATURES
    from .web.core import FeatureRegistry
    from .web.core.db import Database
    from .web.core.paths import db_path
    from .web.features.assemblies.importer import import_boms
    from .web.features.catalog.importer import import_from_minimrp
    from .web.features.contacts.importer import import_contacts

    db = Database(db_path())
    registry = FeatureRegistry()
    registry.register(*FEATURES)
    db.apply_migrations(registry)

    console.print(f"Importing catalog from [bold]{src}[/] → {db_path()}")
    stats = import_from_minimrp(db, src)
    stats.update(import_boms(db, src))  # assembly BOM structure (tblusedin)
    stats.update(import_contacts(db, src))  # suppliers/customers/misc address books
    table = Table(title="Imported")
    table.add_column("Table")
    table.add_column("Rows", justify="right")
    for key, value in stats.items():
        table.add_row(key, str(value))
    console.print(table)


@app.command()
def resolve(
    input: Path = typer.Argument(..., exists=True, readable=True, help="BOM file (.csv/.xlsx)."),
    build_qty: Optional[int] = typer.Option(None, "--build-qty", "-q", help="Number of boards to build."),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Output .xlsx path."),
    sandbox: bool = typer.Option(False, "--sandbox", help="Use the Digi-Key sandbox (mock data) instead of production."),
    currency: Optional[str] = typer.Option(None, help="Override locale currency (e.g. SEK, EUR)."),
    mapping: Optional[Path] = typer.Option(None, "--map", help="Column-mapping YAML override."),
    lookup_file: Optional[Path] = typer.Option(None, "--lookup", help="Device-lookup YAML override."),
    settings_file: Optional[Path] = typer.Option(None, "--settings", help="Settings YAML override."),
    check_stock: Optional[Path] = typer.Option(
        None, "--check-stock", exists=True,
        help="miniMRP database (Data/mrp5data); skips buying parts already in stock. "
             "Defaults to settings 'minimrp_path' if set.",
    ),
    reel_threshold: Optional[float] = typer.Option(
        None, "--reel-threshold",
        help="Buy a full reel when the whole reel costs under this (locale currency). "
             "Default 10000; pass 0 to always use cut tape.",
    ),
    purchase: bool = typer.Option(
        False, "--purchase",
        help="Also write distributor cart CSVs (Digi-Key/Mouser) for the parts that need buying.",
    ),
    dry_run: bool = typer.Option(False, help="Classify and build queries without calling the API."),
):
    """Resolve INPUT against Digi-Key and write a new BOM workbook.

    CLI flags override values from the settings file (config/settings.yaml).
    """
    settings = Settings.load(settings_file)
    mappings = load_column_mappings(mapping)
    lookup = load_lookup(lookup_file)
    lines = load_bom(input, mappings)
    console.print(f"Loaded [bold]{len(lines)}[/] BOM lines from {input.name}")

    # Resolve operational defaults: CLI flag wins, else settings file, else built-in.
    build_qty = build_qty if build_qty is not None else settings.build_qty
    currency = currency or settings.currency
    reel_threshold = settings.reel_threshold if reel_threshold is None else reel_threshold
    stock_path = check_stock or (Path(settings.minimrp_path) if settings.minimrp_path else None)

    stock = None
    if stock_path:
        if not stock_path.exists():
            console.print(f"[red]Error:[/] miniMRP database not found: {stock_path}")
            raise typer.Exit(code=1)
        from .minimrp.reader import load_stock_index

        stock = load_stock_index(stock_path)
        console.print(f"Loaded [bold]{len(stock.items)}[/] stock items from {stock_path.name}")

    if dry_run:
        _dry_run(lines, settings, lookup)
        raise typer.Exit()

    creds = _load_creds(sandbox)
    if currency:
        creds.locale_currency = currency
    from .digikey.client import DigiKeyClient

    client = DigiKeyClient(creds)

    mouser = None
    mo_creds = MouserCredentials.from_env()
    if mo_creds:
        from .mouser.client import MouserClient

        mouser = MouserClient(mo_creds)
        console.print("Mouser enabled as second-choice supplier")

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                  console=console) as progress:
        progress.add_task(f"Resolving against Digi-Key ({creds.locale_currency})…", total=None)
        resolved = resolve_bom(
            lines, client, settings, build_qty, lookup, stock, mouser, reel_threshold
        )

    if output:
        out = output
    elif settings.output_dir:
        out = Path(settings.output_dir) / f"{input.stem}-resolved.xlsx"
    else:
        out = input.with_name(f"{input.stem}-resolved.xlsx")
    out.parent.mkdir(parents=True, exist_ok=True)
    write_report(resolved, out, build_qty=build_qty, currency=creds.locale_currency)
    _print_summary(resolved)
    console.print(f"[green]Wrote[/] {out}")

    if purchase:
        from .cart import purchasable_lines, review_lines, write_carts

        carts = write_carts(resolved, out)
        dk, mo = purchasable_lines(resolved)
        counts = {"Digi-Key": len(dk), "Mouser": len(mo), "Review": len(review_lines(resolved))}
        if not carts:
            console.print("No parts need purchasing (all in stock or unresolved).")
        for key, path in carts.items():
            if key == "Review":
                console.print(f"[yellow]Wrote[/] needs-review list ({counts[key]} lines) → {path}  "
                              "[dim](verify, then add to a cart manually)[/]")
            else:
                console.print(f"[green]Wrote[/] {key} cart ({counts[key]} lines) → {path}  "
                              "[dim](upload to the distributor's List/BOM importer)[/]")


def _dry_run(lines, settings, lookup):
    table = Table(title="Dry run — classification & queries (no API calls)")
    table.add_column("RefDes", style="cyan", no_wrap=True)
    table.add_column("Value/Device")
    table.add_column("Kind")
    table.add_column("Query / note")
    for line in lines:
        plan = plan_line(line, settings, lookup)
        note = plan.query or "—"
        if plan.spec and plan.spec.assumed:
            note += f"  [dim](assumed {', '.join(sorted(set(plan.spec.assumed)))})[/]"
        table.add_row(
            line.refdes_str[:24],
            (line.value or line.device or "")[:28],
            plan.kind.value,
            note,
        )
    console.print(table)


def _print_summary(resolved):
    counts: dict[str, int] = {}
    for line in resolved:
        counts[line.status.value] = counts.get(line.status.value, 0) + 1
    table = Table(title="Resolution summary")
    table.add_column("Status")
    table.add_column("Count", justify="right")
    for status in Status:
        if status.value in counts:
            table.add_row(status.value, str(counts[status.value]))
    console.print(table)


if __name__ == "__main__":
    app()
