from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

from .models import AnalysisRequest, Horizon
from .pipeline import run_analysis

app = typer.Typer(help="Evidence-first stock research. Informational only; not investment advice.")


@app.command()
def analyze(
    ticker: str = typer.Argument(..., help="Ticker symbol, e.g. AAPL"),
    horizon: Horizon = typer.Option(Horizon.M12, help="12m, 3y or 5y"),
    sources: str = typer.Option("yahoo,sec,fidelity,msn", help="Comma-separated source keys"),
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


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Run the HTTP API."""
    import uvicorn  # noqa: PLC0415

    uvicorn.run("stock_forecaster.api:app", host=host, port=port)


if __name__ == "__main__":
    app()
