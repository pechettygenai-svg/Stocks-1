from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

from .models import DEFAULT_SOURCES, AnalysisRequest, Horizon
from .pipeline import run_analysis

app = typer.Typer(help="Evidence-first stock research. Informational only; not investment advice.")


@app.command()
def analyze(
    ticker: str = typer.Argument(..., help="Ticker symbol, e.g. AAPL"),
    horizon: Horizon = typer.Option(Horizon.M12, help="12m, 3y or 5y"),
    sources: str = typer.Option(",".join(DEFAULT_SOURCES), help="Comma-separated source keys"),
    out: Path | None = typer.Option(None, help="Write Markdown report here"),
    json_out: Path | None = typer.Option(None, help="Write full JSON result here"),
    risk_tolerance: str | None = typer.Option(
        None, help="Optional; labels output as profile-aware"
    ),
) -> None:
    request = AnalysisRequest(
        ticker=ticker,
        horizon=horizon,
        include_sources=[s.strip() for s in sources.split(",") if s.strip()],
        risk_tolerance=risk_tolerance,
    )
    result = run_analysis(request, on_status=lambda s: typer.echo(f"[{s.value}]", err=True))
    if result.error:
        typer.echo(f"error: {result.error}", err=True)
        raise typer.Exit(code=1)
    if json_out:
        json_out.write_text(result.model_dump_json(indent=2))
    if out:
        out.write_text(result.report_markdown or "")
        typer.echo(f"report written to {out}", err=True)
    else:
        sys.stdout.write(result.report_markdown or "")
    if result.quality_gate_failures:
        typer.echo("quality gate failures: " + json.dumps(result.quality_gate_failures), err=True)
        raise typer.Exit(code=2)


@app.command("watch-check")
def watch_check(
    name: str | None = typer.Option(None, help="Only this watchlist (default: all)"),
    db: str | None = typer.Option(None, help="SQLite file (default: $STOCK_FORECASTER_DB)"),
) -> None:
    """Re-run watchlists (e.g. from cron). Prints alerts; exit code 3 if any fired."""
    from .store import RunStore  # noqa: PLC0415
    from .watch import check_watchlist  # noqa: PLC0415

    store = RunStore(db)
    lists = [w for w in store.list_watchlists() if name is None or w["name"] == name]
    if not lists:
        typer.echo("no matching watchlists", err=True)
        raise typer.Exit(code=1)
    fired = 0
    for wl in lists:
        for item in check_watchlist(store, wl["id"], lambda req: run_analysis(req)):
            typer.echo(f"{wl['name']}\t{item.ticker}\t{item.status}\t{item.error or ''}")
            for c in item.changes:
                typer.echo(f"  [{c.kind}] {c.message}")
            fired += item.status == "alert"
    if fired:
        raise typer.Exit(code=3)


@app.command()
def serve(
    host: str = "127.0.0.1",
    port: int = 8000,
    db: str = typer.Option(
        None,
        help="SQLite run-history file (default: $STOCK_FORECASTER_DB "
        "or ~/.stock_forecaster/runs.db)",
    ),
) -> None:
    """Run the HTTP API."""
    import os  # noqa: PLC0415

    import uvicorn  # noqa: PLC0415

    if db:
        os.environ["STOCK_FORECASTER_DB"] = db

    uvicorn.run("stock_forecaster.api:app", host=host, port=port)


if __name__ == "__main__":
    app()
