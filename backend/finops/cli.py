"""Command line entry point: ``finops <command>``."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from finops.aws.session import AwsContext, CredentialsUnavailable
from finops.config import get_settings
from finops.model import Scan
from finops.pipeline import ScanOptions, run_scan
from finops.store import ScanStore
from finops.util import human_money

app = typer.Typer(
    name="finops",
    help="AWS FinOps agent: inventory, TCO analysis, and cost reduction recommendations.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

EFFORT_COLORS = {"low": "green", "medium": "yellow", "high": "red"}


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # botocore is extremely chatty at debug level.
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _split(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [part.strip() for part in value.split(",") if part.strip()]


@app.command()
def whoami(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """Show the AWS identity and regions the agent will scan."""
    _configure_logging(verbose)
    settings = get_settings()
    ctx = AwsContext(settings=settings)
    try:
        ctx.verify_credentials()
    except CredentialsUnavailable as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    table = Table(show_header=False, box=None)
    table.add_row("Account", ctx.account_id)
    table.add_row("Alias", ctx.account_alias or "-")
    table.add_row("Profile", settings.aws_profile or "(default credential chain)")
    table.add_row("Default region", ctx.default_region)
    table.add_row("Regions to scan", f"{len(ctx.regions)}: {', '.join(ctx.regions)}")
    table.add_row("Cost lookback", f"{settings.cost_lookback_days} days")
    table.add_row("Metric lookback", f"{settings.metric_lookback_days} days")
    table.add_row("LLM provider", settings.llm_provider)
    console.print(table)


@app.command()
def scan(
    regions: str = typer.Option(None, "--regions", "-r", help="Comma-separated region list."),
    collectors: str = typer.Option(None, "--collectors", help="Only run these collectors."),
    skip_collectors: str = typer.Option(None, "--skip-collectors"),
    rules: str = typer.Option(None, "--rules", help="Only run these rules."),
    skip_rules: str = typer.Option(None, "--skip-rules"),
    no_metrics: bool = typer.Option(False, "--no-metrics", help="Skip CloudWatch collection."),
    no_native: bool = typer.Option(False, "--no-native", help="Skip AWS recommendation APIs."),
    no_advice: bool = typer.Option(False, "--no-advice", help="Skip the LLM advisor."),
    no_save: bool = typer.Option(False, "--no-save", help="Do not persist to the scan store."),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Scan a mocked account instead of AWS. No credentials, no API charges.",
    ),
    output: Path = typer.Option(None, "--output", "-o", help="Also write the scan as JSON."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run a read-only scan and store the result."""
    _configure_logging(verbose)
    settings = get_settings()

    if dry_run:
        from finops.demo import run_demo_scan

        console.print(
            "[yellow]Dry run:[/yellow] inventory comes from a mocked account, and cost and "
            "utilization figures are synthetic."
        )
        with console.status("[bold]Scanning mock account...", spinner="dots") as status:
            result = run_demo_scan(
                settings,
                persist=not no_save,
                with_advice=not no_advice,
                progress=lambda stage, message: status.update(f"[bold]{stage}[/bold]: {message}"),
            )
        _print_report(result)
        console.print(f"\nScan id: [bold]{result.scan_id}[/bold]")
        return

    try:
        AwsContext(settings=settings).verify_credentials()
    except CredentialsUnavailable as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    options = ScanOptions(
        regions=_split(regions),
        collectors=_split(collectors),
        skip_collectors=_split(skip_collectors) or [],
        rules=_split(rules),
        skip_rules=_split(skip_rules) or [],
        with_metrics=not no_metrics,
        with_native=not no_native,
        with_advice=not no_advice,
        persist=not no_save,
    )

    with console.status("[bold]Scanning...", spinner="dots") as status:
        result = run_scan(
            settings,
            options,
            progress=lambda stage, message: status.update(f"[bold]{stage}[/bold]: {message}"),
        )

    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        console.print(f"Wrote {output}")

    _print_report(result)
    console.print(f"\nScan id: [bold]{result.scan_id}[/bold]")


@app.command()
def report(
    scan_id: str = typer.Argument("latest", help="Scan id, or 'latest'."),
    json_output: bool = typer.Option(False, "--json", help="Print the raw scan JSON."),
    limit: int = typer.Option(15, "--limit", "-n", help="Findings to show."),
) -> None:
    """Print a stored scan."""
    store = ScanStore(get_settings().db_path)
    resolved = store.resolve_scan_id(scan_id)
    if resolved is None:
        console.print("[yellow]No scans found. Run `finops scan` first.[/yellow]")
        raise typer.Exit(code=1)
    result = store.get_scan(resolved)
    if result is None:
        console.print(f"[red]Unknown scan: {scan_id}[/red]")
        raise typer.Exit(code=1)

    if json_output:
        console.print_json(result.model_dump_json())
        return
    _print_report(result, limit=limit)


@app.command()
def scans(limit: int = typer.Option(20, "--limit", "-n")) -> None:
    """List stored scans."""
    store = ScanStore(get_settings().db_path)
    history = store.list_scans(limit=limit)
    if not history:
        console.print("[yellow]No scans yet.[/yellow]")
        return

    table = Table(title="Scan history")
    table.add_column("Scan id")
    table.add_column("Started")
    table.add_column("Kind")
    table.add_column("Resources", justify="right")
    table.add_column("Findings", justify="right")
    table.add_column("Run rate/mo", justify="right")
    table.add_column("Savings/mo", justify="right")
    for meta in history:
        if meta.dry_run:
            kind = "[magenta]demo[/magenta]"
        elif meta.is_empty:
            kind = "[yellow]empty[/yellow]"
        else:
            kind = "live"
        table.add_row(
            meta.scan_id,
            meta.started_at.strftime("%Y-%m-%d %H:%M"),
            kind,
            str(meta.resource_count),
            str(meta.finding_count),
            human_money(meta.monthly_run_rate),
            human_money(meta.identified_monthly_savings),
        )
    console.print(table)
    console.print(
        "[dim]finops prune --demo --empty removes the demo and failed runs.[/dim]",
    )


@app.command()
def advise(
    scan_id: str = typer.Argument("latest"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Regenerate architectural advice for a stored scan without rescanning."""
    _configure_logging(verbose)
    from finops.pipeline import regenerate_advice

    settings = get_settings()
    store = ScanStore(settings.db_path)
    resolved = store.resolve_scan_id(scan_id)
    result = store.get_scan(resolved) if resolved else None
    if result is None:
        console.print("[red]No such scan.[/red]")
        raise typer.Exit(code=1)

    with console.status("[bold]Asking the model..."):
        regenerate_advice(result, settings, store=store)
    _print_advice(result)


@app.command()
def prune(
    keep: int | None = typer.Option(
        None, "--keep", "-k", help="Retain this many of the most recent scans, drop the rest."
    ),
    demo: bool = typer.Option(False, "--demo", help="Drop scans produced by --dry-run."),
    empty: bool = typer.Option(
        False, "--empty", help="Drop scans that collected nothing, usually a failed run."
    ),
    scan_ids: list[str] = typer.Option([], "--id", help="Drop a specific scan. Repeatable."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Delete stored scans, by age, by kind, or by id.

    With no options this keeps the 10 most recent scans, which is the old behaviour.
    """
    store = ScanStore(get_settings().db_path)
    selective = demo or empty or scan_ids
    if not selective and keep is None:
        keep = 10

    history = store.list_scans(limit=10_000)
    doomed: dict[str, str] = {}
    if demo:
        doomed.update({m.scan_id: "demo" for m in history if m.dry_run})
    if empty:
        doomed.update({m.scan_id: "empty" for m in history if m.is_empty})
    for scan_id in scan_ids:
        resolved = store.resolve_scan_id(scan_id)
        if resolved is None or store.get_scan_meta(resolved) is None:
            console.print(f"[yellow]No scan {scan_id}; skipping.[/yellow]")
            continue
        doomed[resolved] = "requested"
    if keep is not None:
        # Age applies to whatever the other filters left behind, so --demo --keep 5 means
        # "drop the demos, then keep the 5 newest of what remains".
        survivors = [m for m in history if m.scan_id not in doomed]
        doomed.update({m.scan_id: "old" for m in survivors[keep:]})

    if not doomed:
        console.print("Nothing to prune.")
        return

    table = Table(title=f"{len(doomed)} scan(s) to delete")
    table.add_column("Scan id")
    table.add_column("Started")
    table.add_column("Reason")
    table.add_column("Resources", justify="right")
    for meta in history:
        if meta.scan_id in doomed:
            table.add_row(
                meta.scan_id,
                meta.started_at.strftime("%Y-%m-%d %H:%M"),
                doomed[meta.scan_id],
                str(meta.resource_count),
            )
    console.print(table)

    if not yes and not typer.confirm("Delete these scans?"):
        console.print("Left alone.")
        return

    removed = store.delete_scans(list(doomed))
    console.print(f"Removed {removed} scan(s); {len(history) - removed} remain.")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port", "-p"),
    reload: bool = typer.Option(False, "--reload"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Serve the API and, if it has been built, the dashboard."""
    _configure_logging(verbose)
    import uvicorn

    console.print(f"API on [bold]http://{host}:{port}/api[/bold] (docs at /docs)")
    uvicorn.run(
        "finops.api:create_app",
        host=host,
        port=port,
        reload=reload,
        factory=True,
    )


@app.command("policy")
def show_policy() -> None:
    """Print the read-only IAM policy the agent needs."""
    path = Path(__file__).resolve().parents[2] / "iam" / "finops-readonly-policy.json"
    console.print_json(json.dumps(json.loads(path.read_text(encoding="utf-8"))))


def _print_report(result: Scan, limit: int = 15) -> None:
    tco = result.tco
    if tco is None:
        console.print("[yellow]Scan has no TCO report.[/yellow]")
        return

    summary = Table(show_header=False, box=None)
    summary.add_row("Account", f"{result.account_id} ({result.account_alias or 'no alias'})")
    summary.add_row("Period", f"{tco.period_start} to {tco.period_end}")
    summary.add_row("Billed in period", human_money(tco.total_cost))
    summary.add_row("Monthly run rate", human_money(tco.monthly_run_rate))
    if tco.forecast_next_month is not None:
        summary.add_row("Forecast next month", human_money(tco.forecast_next_month))
    summary.add_row(
        "Identified savings",
        f"[green]{human_money(tco.identified_monthly_savings)}/mo "
        f"({tco.savings_percent:.1f}%)[/green]",
    )
    summary.add_row("Optimized run rate", human_money(tco.optimized_monthly_run_rate))
    summary.add_row("Resources", str(len(result.resources)))
    console.print(Panel(summary, title="Total cost of ownership", expand=False))

    if tco.by_service:
        table = Table(title="Cost by service")
        table.add_column("Service")
        table.add_column("Monthly", justify="right")
        table.add_column("Share", justify="right")
        table.add_column("Identified savings", justify="right")
        for item in tco.by_service[:10]:
            table.add_row(
                item.key,
                human_money(item.amount),
                f"{item.share:.1f}%",
                human_money(item.savings) if item.savings else "-",
            )
        console.print(table)

    if result.findings:
        table = Table(title=f"Top findings (of {len(result.findings)})")
        table.add_column("Savings/mo", justify="right")
        table.add_column("Finding")
        table.add_column("Resource")
        table.add_column("Effort")
        table.add_column("Source")
        for finding in result.findings[:limit]:
            color = EFFORT_COLORS[finding.implementation_effort]
            table.add_row(
                human_money(finding.estimated_monthly_savings),
                finding.title,
                finding.resource_id or finding.region or "-",
                f"[{color}]{finding.implementation_effort}[/{color}]",
                finding.source,
            )
        console.print(table)

    _print_advice(result)

    problems = [note for note in result.notes if note.status != "ok"]
    if problems:
        table = Table(title="Data sources unavailable")
        table.add_column("Capability")
        table.add_column("Status")
        table.add_column("Detail")
        for note in problems[:10]:
            table.add_row(note.capability, note.status, (note.remedy or note.message)[:80])
        console.print(table)


def _print_advice(result: Scan) -> None:
    advice = result.advice
    if advice is None or not advice.executive_summary:
        return
    console.print(
        Panel(
            advice.executive_summary,
            title=f"Executive summary ({advice.provider or 'deterministic'})",
        )
    )
    for index, recommendation in enumerate(advice.recommendations, start=1):
        savings = (
            f" ~{human_money(recommendation.estimated_monthly_savings)}/mo"
            if recommendation.estimated_monthly_savings
            else ""
        )
        console.print(
            f"[bold]{index}. {recommendation.title}[/bold]{savings} "
            f"[dim]({recommendation.implementation_effort} effort, "
            f"{recommendation.risk} risk)[/dim]"
        )
        console.print(f"   {recommendation.summary}")


if __name__ == "__main__":
    app()
