"""Citation-constrained Markdown report. Every number comes from the ledger or
a calculation; prose (optional LLM) is limited to the executive view and is
checked for citations by the quality gates."""

from __future__ import annotations

from .ledger import EvidenceLedger
from .llm.provider import LLMError, LLMProvider
from .models import DISCLAIMER, AnalysisResult, EvidenceRecord, RetrievalStatus


def _n(x: object, digits: int = 2) -> str:
    if not isinstance(x, (int, float)):
        return "n/a"
    if abs(x) >= 1e9:
        return f"{x / 1e9:,.2f}B"
    if abs(x) >= 1e6:
        return f"{x / 1e6:,.1f}M"
    return f"{x:,.{digits}f}"


def _p(x: object) -> str:
    return f"{x:+.1%}" if isinstance(x, (int, float)) else "n/a"


def _c(rec: EvidenceRecord | None) -> str:
    return rec.cite() if rec else ""


def _line(ledger: EvidenceLedger, label: str, field: str, fmt: str = "n") -> str | None:
    rec = ledger.best(field)
    if rec is None:
        return None
    v = rec.value
    shown = _p(v) if fmt == "p" else (_n(v) if fmt == "n" else str(v))
    if rec.unit and fmt == "n" and rec.currency:
        shown += f" {rec.currency}"
    return f"- **{label}:** {shown} (as of {rec.as_of}) {rec.cite()}"


def render_report(result: AnalysisResult, ledger: EvidenceLedger, provider: LLMProvider) -> str:
    req = result.request
    price = ledger.best("price_intraday")
    name = result.company_name or req.ticker
    calcs = {c.name: c for c in result.calculations}
    tech = calcs.get("technicals")
    fund = calcs.get("fundamentals")
    val = calcs.get("valuation")
    scen = calcs.get("scenarios")
    lines: list[str] = []

    lines.append(f"# {name} ({req.ticker.upper()}) — Evidence-Based Stock Analysis")
    lines.append(f"> {DISCLAIMER}")
    lines.append("")
    unavailable = [
        r.source_name for r in ledger.all() if r.retrieval_status == RetrievalStatus.UNAVAILABLE
    ]
    verified = sorted(
        {
            r.source_name.split(" (")[0]
            for r in ledger.all()
            if r.retrieval_status == RetrievalStatus.VERIFIED
        }
    )
    lines.append(
        f"**As of:** {result.started_at:%Y-%m-%d %H:%M UTC} · **Horizon:** {req.horizon.value} · "
        f"**Data coverage:** verified: {', '.join(verified) or 'none'}; unavailable: "
        f"{', '.join(sorted(set(unavailable))) or 'none'} · **Evidence fingerprint:** "
        f"`{ledger.fingerprint()}`"
    )
    if req.risk_tolerance:
        lines.append(
            f"\n_Profile-aware perspective (stated risk tolerance: {req.risk_tolerance}). "
            "This does not constitute a personalized recommendation._"
        )

    # Executive view --------------------------------------------------------
    lines.append("\n## Executive view")
    lines.append(_executive_view(result, ledger, provider))

    # Snapshot --------------------------------------------------------------
    lines.append("\n## Current snapshot")
    for label, field, fmt in (
        ("Last price", "price_intraday", "n"),
        ("Exchange", "exchange", "s"),
        ("Sector", "sector", "s"),
        ("Industry", "industry", "s"),
        ("Market cap", "market_cap", "n"),
        ("52-week low", "week52_low", "n"),
        ("52-week high", "week52_high", "n"),
        ("Trailing P/E", "pe_ttm", "n"),
        ("Forward P/E", "pe_forward", "n"),
        ("Dividend yield", "dividend_yield", "p"),
        ("Short % of float", "short_pct_float", "p"),
    ):
        ln = _line(ledger, label, field, fmt)
        if ln:
            lines.append(ln)

    # Scenarios -------------------------------------------------------------
    lines.append(f"\n## Scenario range ({req.horizon.value})")
    mode = scen.outputs.get("mode") if scen else "earnings"
    lines.append(
        "Probabilities are neutral starting assumptions (25/50/25), not forecasts. "
        f"Valuation basis: exit multiple on {mode}. Inputs cite "
        + " ".join(
            _c(ledger.best(f))
            for f in (
                "price_intraday",
                "revenue_ttm",
                "revenue_fy",
                "eps_forward",
                "pe_forward",
                "diluted_shares_fy",
            )
            if ledger.best(f)
        )
        + "."
    )
    lines.append("")
    lines.append(
        "| Scenario | Driver assumptions | Implied value range | Probability (assumed) | "
        "What must be true |"
    )
    lines.append("|---|---|---|---|---|")
    for s in result.scenarios:
        d = s.drivers
        drv = (
            f"rev growth {d.revenue_growth:+.1%}/yr; margin {d.operating_margin:.1%}; "
            f"shares {d.share_change:+.1%}/yr; exit {d.exit_multiple:.1f}x"
        )
        rng = (
            f"{_n(s.implied_price_low)} – {_n(s.implied_price_high)} (mid {_n(s.implied_price_mid)}"
            + (
                f", {_p(s.implied_price_mid / float(price.value) - 1)} vs price)"
                if price and s.implied_price_mid and isinstance(price.value, float)
                else ")"
            )
        )
        lines.append(
            f"| {s.name} | {drv} | {rng} | {s.probability:.0%} | {'; '.join(s.what_must_be_true)} |"
        )
    if scen:
        pw = scen.outputs.get("probability_weighted_mid")
        if isinstance(pw, float):
            lines.append(
                f"\nProbability-weighted midpoint: {_n(pw)} — an arithmetic summary of the "
                "assumptions above, not a prediction of what will happen."
            )
        sens = scen.outputs.get("sensitivity_base")
        if isinstance(sens, dict):
            lines.append(
                "\n**Sensitivity (base case): implied value by revenue-growth shift × "
                "exit-multiple factor**\n"
            )
            cols = list(next(iter(sens.values())).keys())
            lines.append("| growth shift | " + " | ".join(cols) + " |")
            lines.append("|---|" + "---|" * len(cols))
            for k, row in sens.items():
                lines.append(f"| {k} | " + " | ".join(_n(v) for v in row.values()) + " |")
        for w in scen.warnings:
            lines.append(f"- ⚠ {w}")
    inval = [x for ro in result.role_outputs for x in ro.invalidated_if]
    if inval:
        lines.append("\n**Thesis invalidated if:**")
        lines += [f"- {x}" for x in dict.fromkeys(inval)]

    # Business --------------------------------------------------------------
    lines.append("\n## What the company does")
    sector, industry = ledger.best("sector"), ledger.best("industry")
    summary = ledger.best("business_summary")
    if summary and isinstance(summary.value, str):
        lines.append(f"{summary.value} {summary.cite()}")
    if sector or industry:
        lines.append(
            f"\n{name} is classified under {sector.value if sector else 'n/a'} / "
            f"{industry.value if industry else 'n/a'} {_c(sector)} {_c(industry)}. "
            "Description is provider-supplied, not from a filing (see Limitations)."
        )
    elif not summary:
        lines.append("No sector/industry classification retrieved.")

    # Fundamentals & valuation ---------------------------------------------
    lines.append("\n## Fundamentals and valuation")
    for label, field in (
        ("Revenue (FY)", "revenue_fy"),
        ("Revenue (prior FY)", "revenue_fy_prior"),
        ("Operating income (FY)", "operating_income_fy"),
        ("Net income (FY)", "net_income_fy"),
        ("Diluted EPS (FY)", "eps_diluted_fy"),
        ("Operating cash flow (FY)", "operating_cash_flow_fy"),
        ("Capex (FY)", "capex_fy"),
        ("Cash", "cash"),
        ("Long-term debt", "long_term_debt"),
        ("Equity", "total_equity"),
        ("Diluted shares (FY)", "diluted_shares_fy"),
        ("Net interest income (FY)", "net_interest_income_fy"),
        ("Provision for credit losses (FY)", "provision_credit_losses_fy"),
        ("Net loans", "loans_net"),
        ("Deposits", "deposits"),
    ):
        ln = _line(ledger, label, field)
        if ln:
            lines.append(ln)
    if fund:
        o = fund.outputs
        ids = " ".join(f"[{i}]" for i in fund.evidence_ids[:6])
        lines.append(f"\n**Recomputed ratios** (formula {fund.formula_version}; inputs {ids}):")
        for k in (
            "revenue_growth_yoy",
            "net_income_growth_yoy",
            "operating_margin",
            "net_margin",
            "fcf_margin",
            "roe",
            "roa",
            "share_count_change_yoy",
            "fcf_yield",
            "earnings_yield_ttm",
            "nii_to_revenue",
            "provision_to_loans",
            "loans_to_deposits",
            "equity_to_assets",
        ):
            if isinstance(o.get(k), float):
                lines.append(f"- {k}: {_p(o[k])}")
        for k in (
            "free_cash_flow",
            "net_debt",
            "pe_ttm_recomputed",
            "pe_forward_recomputed",
            "price_to_book_recomputed",
            "cash_conversion_fcf_to_ni",
        ):
            if isinstance(o.get(k), float):
                lines.append(f"- {k}: {_n(o[k])}")
        for flag in o.get("red_flags", []):
            lines.append(f"- ⚠ {flag}")
    if val:
        o = val.outputs
        lines.append(
            f"\n**Valuation models** (formula {val.formula_version}; discount rate "
            f"{o['discount_rate']:.1%}, terminal growth {o['terminal_growth']:.1%} — "
            "assumptions, not observed data):"
        )
        grid = o.get("dcf_per_share_by_growth")
        if isinstance(grid, dict):
            lines.append(
                "- DCF per share by 5-yr FCF growth: "
                + ", ".join(
                    f"{k.replace('fcf_growth_', '').replace('pct', '%')}: {_n(v)}"
                    for k, v in grid.items()
                )
            )
        if isinstance(o.get("implied_fcf_growth_at_market_price"), float):
            lines.append(
                f"- Reverse DCF implied FCF growth at market price: "
                f"{_p(o['implied_fcf_growth_at_market_price'])}/yr"
            )
        if isinstance(o.get("price_at_forward_pe"), dict):
            lines.append(
                "- Price at forward P/E: "
                + ", ".join(f"{k}: {_n(v)}" for k, v in o["price_at_forward_pe"].items())
            )
        for w in val.warnings:
            lines.append(f"- ⚠ {w}")
    lines += _role_section(result, ("fundamentals", "valuation"))

    # Technicals ------------------------------------------------------------
    lines.append("\n## Technical context")
    lines.append("Indicators are descriptive context, not predictions.")
    if tech:
        o = tech.outputs
        ids = " ".join(f"[{i}]" for i in tech.evidence_ids)
        lines.append(
            f"\n**Indicators** (formula {tech.formula_version}; inputs {ids}; "
            f"as of {price.as_of if price else 'n/a'}):"
        )
        lines.append(f"- Trend: {o['trend_label']}")
        lines.append(
            f"- 50d SMA {_n(o['sma_50'])} ({_p(o['price_vs_sma50'])} vs price); "
            f"200d SMA {_n(o['sma_200'])} ({_p(o['price_vs_sma200'])} vs price)"
        )
        lines.append(
            f"- RSI(14) {_n(o['rsi_14'], 1)}; 1y max drawdown {_p(o['max_drawdown_1y'])}; "
            f"annualized volatility {_p(o['annualized_volatility'])}; "
            f"period return {_p(o['total_return_period'])}"
        )
        lines.append(
            f"- 1y low/high (support/resistance context): {_n(o['support_1y_low'])} / "
            f"{_n(o['resistance_1y_high'])}; volume ratio 10d/50d {_n(o['volume_ratio_10_50'])}"
        )
        for w in tech.warnings:
            lines.append(f"- ⚠ {w}")
    lines += _role_section(result, ("technical",))

    # Forecast comparison ---------------------------------------------------
    lines.append("\n## External forecast comparison")
    lines.append(
        "| Source | Forecast type | Horizon | Value/range | Date | Analysts | "
        "Method disclosed? | Weight | Flags | Evidence |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for f in result.forecasts:
        rng = (
            "unavailable"
            if f.status == RetrievalStatus.UNAVAILABLE
            else f"{_n(f.value_low)} / {_n(f.value_mean)} / {_n(f.value_high)} (low/mean/high)"
        )
        flags = [
            *(["duplicate of " + f.duplicate_of] if f.duplicate_of else []),
            *(["stale"] if f.stale else []),
            *(["outlier"] if f.outlier else []),
            *(["user-supplied"] if f.status == RetrievalStatus.SNIPPET_ONLY else []),
        ]
        lines.append(
            f"| {f'[{f.source}]({f.url})' if f.url else f.source} | {f.forecast_type} | {f.horizon} | {rng} | "
            f"{f.as_of or 'n/a'} | {f.analyst_count or 'n/a'} | "
            f"{'yes' if f.method_disclosed else 'no'} | {f.weight} | "
            f"{', '.join(flags) or '—'} | {' '.join(f'[{i}]' for i in f.evidence_ids)} |"
        )
    lines.append(
        "\nValues from different sources are not averaged; consensus figures are opaque "
        "aggregates and may share underlying analysts."
    )
    lines += _role_section(result, ("forecast_auditor",))

    # Catalysts & risks -----------------------------------------------------
    lines.append("\n## Catalysts and risks")
    lines += _role_section(result, ("catalyst_risk", "skeptic"))

    # Review notes ----------------------------------------------------------
    if result.reviews:
        lines.append("\n## Review notes")
        lines.append(
            f"Critic and auditor pass (revision {result.revision_count}). Findings and how "
            "the draft was revised:"
        )
        lines.append("| Reviewer | Check | Severity | Finding | Resolution |")
        lines.append("|---|---|---|---|---|")
        for rv in result.reviews:
            lines.append(
                f"| {rv.reviewer} | {rv.check} | {rv.severity.value} | {rv.message} | "
                f"{rv.resolution or 'open'} |"
            )

    # Limitations -----------------------------------------------------------
    lines.append("\n## Limitations")
    lines.append("- Data gaps and validation warnings:")
    seen: set[str] = set()
    for w in result.validation_warnings:
        if w not in seen:
            seen.add(w)
            lines.append(f"  - {w}")
    for c in result.calculations:
        for w in c.warnings:
            if w not in seen:
                seen.add(w)
                lines.append(f"  - [{c.name}] {w}")
    oq = [q for ro in result.role_outputs for q in ro.open_questions]
    if oq:
        lines.append("- Open questions:")
        lines += [f"  - {q}" for q in dict.fromkeys(oq)]
    lines.append(
        "- Scenario drivers are mechanical extrapolations of one or two fiscal years and "
        "current multiples; they are assumptions to be argued with, not forecasts."
    )
    open_reviews = [rv for rv in result.reviews if rv.resolution is None]
    if open_reviews:
        lines.append("- Unresolved review findings:")
        lines += [f"  - [{rv.reviewer}/{rv.severity.value}] {rv.message}" for rv in open_reviews]
    lines.append(
        "- Analyst roles ran with model: "
        + ", ".join(sorted({ro.model or "n/a" for ro in result.role_outputs}))
        + "."
    )

    # Sources ---------------------------------------------------------------
    lines.append("\n## Sources")
    lines.append("| ID | Claim | Value | As of | Source | Type | Tier | Status |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in ledger.all():
        src = f"[{r.source_name}]({r.source_url})" if r.source_url else r.source_name
        val_s = _n(r.value) if isinstance(r.value, float) else (r.value or "")
        lines.append(
            f"| {r.id} | {r.claim} | {val_s} | {r.as_of or ''} | {src} | "
            f"{r.source_type.value} | {r.tier} | {r.retrieval_status.value} |"
        )
    lines.append(f"\n_{DISCLAIMER}_")
    return "\n".join(lines)


def _role_section(result: AnalysisResult, roles: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for ro in result.role_outputs:
        if ro.role not in roles or not ro.findings:
            continue
        out.append(f"\n**{ro.role.replace('_', ' ').title()} analyst** ({ro.model}):")
        for f in ro.findings:
            ids = " ".join(f"[{i}]" for i in f.evidence_ids)
            cav = f" _({f.caveat})_" if f.caveat else ""
            out.append(
                f"- [{f.type.value}] {f.statement} {ids} (confidence {f.confidence:.1f}){cav}"
            )
    return out


def _executive_view(result: AnalysisResult, ledger: EvidenceLedger, provider: LLMProvider) -> str:
    price = ledger.best("price_intraday")
    base = next((s for s in result.scenarios if s.name == "Base"), None)
    bear = next((s for s in result.scenarios if s.name == "Bear"), None)
    bull = next((s for s in result.scenarios if s.name == "Bull"), None)
    fund = next((c for c in result.calculations if c.name == "fundamentals"), None)
    tech = next((c for c in result.calculations if c.name == "technicals"), None)
    n_unavail = sum(1 for r in ledger.all() if r.retrieval_status == RetrievalStatus.UNAVAILABLE)
    n_ver = sum(1 for r in ledger.all() if r.retrieval_status == RetrievalStatus.VERIFIED)
    confidence = "moderate" if n_ver >= 15 and ledger.best("revenue_fy") else "low"

    facts: list[str] = []
    if price:
        facts.append(
            f"last price {_n(price.value)} {price.currency or ''} as of {price.as_of} "
            f"{price.cite()}"
        )
    if fund and isinstance(fund.outputs.get("revenue_growth_yoy"), float):
        facts.append(
            f"filing-basis revenue growth {_p(fund.outputs['revenue_growth_yoy'])} "
            f"{' '.join(f'[{i}]' for i in fund.evidence_ids[:2])}"
        )
    if fund and isinstance(fund.outputs.get("net_margin"), float):
        facts.append(f"net margin {_p(fund.outputs['net_margin'])}")
    if tech:
        facts.append(
            f"trend: {tech.outputs['trend_label']} {' '.join(f'[{i}]' for i in tech.evidence_ids)}"
        )
    rng = ""
    if bear and bull and bear.implied_price_mid and bull.implied_price_mid and base:
        rng = (
            f" Under the stated drivers the {result.request.horizon.value} scenario range is "
            f"{_n(bear.implied_price_low)}–{_n(bull.implied_price_high)} with a base midpoint of "
            f"{_n(base.implied_price_mid)}."
        )

    deterministic = (
        f"Evidence-based view with **{confidence} confidence** ({n_ver} verified records, "
        f"{n_unavail} unavailable sources). Key facts: "
        + "; ".join(facts)
        + "."
        + rng
        + " The range reflects assumptions listed below; sources disagree and are shown side by side "
        "rather than averaged."
    )
    try:
        cites = [r.id for r in ledger.all() if r.retrieval_status == RetrievalStatus.VERIFIED]
        prose = provider.text(
            "Write a 3-5 sentence executive view: conclusion with a confidence label, the 2-4 facts "
            "that drive it, and the scenario range. Cite evidence ids. No recommendation language.",
            ledger.all(),
            cites,
            {
                "scenarios": [s.model_dump(mode="json") for s in result.scenarios],
                "calculations": {c.name: c.outputs for c in result.calculations},
            },
        )
        return prose.strip() + "\n\n_Deterministic summary:_ " + deterministic
    except LLMError:
        return deterministic
