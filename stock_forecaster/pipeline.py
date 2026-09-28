"""Orchestrator: collect -> validate -> model -> review -> synthesize -> gate."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from .adapters import EvidenceAdapter, SecEdgarAdapter, YahooAdapter, fidelity_adapter, msn_adapter
from .analytics import (
    ScenarioInputs,
    build_scenarios,
    fundamental_summary,
    technical_summary,
    valuation_summary,
)
from .ledger import EvidenceLedger
from .llm import LLMProvider, get_provider, run_role
from .llm.roles import ROLE_TASKS
from .models import AnalysisRequest, AnalysisResult, RunStatus, utc_now
from .quality import run_quality_gates
from .report import render_report
from .validation import forecast_panel, validate

StatusCallback = Callable[[RunStatus], None]


def default_adapters(request: AnalysisRequest) -> list[EvidenceAdapter]:
    registry: dict[str, Callable[[], EvidenceAdapter]] = {
        "yahoo": YahooAdapter,
        "sec": SecEdgarAdapter,
        "fidelity": fidelity_adapter,
        "msn": msn_adapter,
    }
    return [registry[s]() for s in request.include_sources if s in registry]


def run_analysis(
    request: AnalysisRequest,
    adapters: list[EvidenceAdapter] | None = None,
    provider: LLMProvider | None = None,
    on_status: StatusCallback | None = None,
) -> AnalysisResult:
    provider = provider or get_provider()
    adapters = adapters if adapters is not None else default_adapters(request)
    result = AnalysisResult(
        run_id=f"run-{uuid.uuid4().hex[:12]}",
        request=request,
        status=RunStatus.COLLECTING,
        started_at=utc_now(),
    )
    ledger = EvidenceLedger()
    ticker = request.ticker.upper().strip()

    def status(s: RunStatus) -> None:
        result.status = s
        if on_status:
            on_status(s)

    try:
        # 1. collect in parallel -------------------------------------------
        status(RunStatus.COLLECTING)
        with ThreadPoolExecutor(max_workers=len(adapters) or 1) as pool:
            batches = list(pool.map(lambda a: a.collect(ticker), adapters))
        for batch in batches:
            ledger.extend(batch)
        yahoo = next((a for a in adapters if isinstance(a, YahooAdapter)), None)
        snapshot = yahoo.snapshot if yahoo else None
        result.company_name = (
            snapshot.company_name if snapshot and snapshot.company_name else ticker
        )
        result.currency = snapshot.currency if snapshot else None

        # 2. validate --------------------------------------------------------
        status(RunStatus.VALIDATING)
        result.validation_warnings = validate(ledger)
        result.forecasts = forecast_panel(ledger, request.horizon.value)

        # 3. deterministic analytics -----------------------------------------
        status(RunStatus.MODELING)
        calcs = {}
        if snapshot and snapshot.closes:
            calcs["technicals"] = technical_summary(
                snapshot.closes,
                snapshot.volumes,
                [r.id for r in ledger.by_field("price_history")],
            )
        fund = fundamental_summary(ledger)
        calcs["fundamentals"] = fund
        price = ledger.value("price_intraday")
        shares = ledger.value("diluted_shares_fy") or ledger.value("shares_outstanding")
        fcf = fund.outputs.get("free_cash_flow")
        net_debt = fund.outputs.get("net_debt")
        calcs["valuation"] = valuation_summary(
            price,
            fcf if isinstance(fcf, float) else None,
            net_debt if isinstance(net_debt, float) else (0.0 if fcf is not None else None),
            shares,
            ledger.value("eps_forward"),
            fund.evidence_ids,
        )
        if net_debt is None and fcf is not None:
            calcs["valuation"].warnings.append("net debt unavailable; DCF assumes zero net debt")

        if price is not None:
            # Scenario base prefers the most recent period (TTM) over the last FY.
            revenue = ledger.value("revenue_ttm") or ledger.value("revenue_fy")
            mcap = ledger.value("market_cap")
            nm = fund.outputs.get("net_margin")
            pe = ledger.value("pe_forward") or ledger.value("pe_ttm")
            inp = ScenarioInputs(
                price=price,
                revenue=revenue,
                net_margin=nm if isinstance(nm, float) else None,
                shares=shares,
                eps=ledger.value("eps_forward") or ledger.value("eps_ttm"),
                revenue_growth_hist=fund.outputs.get("revenue_growth_yoy")
                if isinstance(fund.outputs.get("revenue_growth_yoy"), float)
                else None,
                pe_current=pe,
                ps_current=(mcap / revenue) if mcap and revenue else None,
                horizon_years=request.horizon.years,
            )
            result.scenarios, calcs["scenarios"] = build_scenarios(inp)
        result.calculations = list(calcs.values())

        # 4. specialist roles + review ----------------------------------------
        status(RunStatus.REVIEWING)
        with ThreadPoolExecutor(max_workers=6) as pool:
            result.role_outputs = list(
                pool.map(
                    lambda role: run_role(
                        role, provider, ledger, calcs, result.scenarios, request.horizon.value
                    ),
                    ROLE_TASKS.keys(),
                )
            )

        # 5. synthesize + gates -----------------------------------------------
        result.evidence = ledger.all()
        result.report_markdown = render_report(result, ledger, provider)
        result.quality_gate_failures = run_quality_gates(result, ledger)
        if result.quality_gate_failures and provider.name != "none":
            # Model prose failed a gate: drop it and use deterministic synthesis.
            from .llm import NullProvider  # noqa: PLC0415

            result.report_markdown = render_report(result, ledger, NullProvider())
            result.quality_gate_failures = run_quality_gates(result, ledger)
        result.status = RunStatus.COMPLETE
    except Exception as exc:  # pragma: no cover - defensive
        result.status = RunStatus.FAILED
        result.error = f"{type(exc).__name__}: {exc}"
        result.evidence = ledger.all()
    result.finished_at = utc_now()
    return result
