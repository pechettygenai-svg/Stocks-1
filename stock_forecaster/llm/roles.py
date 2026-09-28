"""Bounded specialist roles. Each returns a RoleOutput whose findings must cite
existing evidence ids. With no provider configured, deterministic templates
derived from the calculation outputs are used instead."""

from __future__ import annotations

from typing import Any

from ..ledger import EvidenceLedger
from ..models import CalculationResult, Finding, FindingType, RoleOutput, Scenario
from .provider import LLMError, LLMProvider

ROLE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "statement": {"type": "string"},
                    "type": {"enum": ["fact", "source_opinion", "model_output", "inference"]},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "caveat": {"type": ["string", "null"]},
                },
                "required": ["statement", "type", "evidence_ids", "confidence"],
            },
        },
        "open_questions": {"type": "array", "items": {"type": "string"}},
        "invalidated_if": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["findings", "open_questions", "invalidated_if"],
}

ROLE_TASKS: dict[str, str] = {
    "fundamentals": "Assess business quality, growth, margins, cash conversion, balance sheet, "
    "dilution and accounting red flags from the evidence and computed ratios.",
    "valuation": "Assess valuation using the computed multiples, DCF grid and reverse-DCF "
    "implied growth. State assumptions explicitly. Give ranges, not points.",
    "technical": "Describe trend, moving averages, RSI, drawdown, volatility and support/"
    "resistance as context only. Avoid any directional certainty.",
    "catalyst_risk": "List catalysts and risks (earnings, product, regulation, competition, "
    "macro, litigation, execution) and concrete thesis-invalidation conditions.",
    "forecast_auditor": "Audit external forecasts: age, methodology disclosure, analyst count, "
    "duplication across aggregators, outliers, and definition mismatches.",
    "skeptic": "Attack the base and bull cases. Identify unsupported claims and force a "
    "specific, measurable bear case.",
}


def _validate_citations(out: dict[str, Any], ledger: EvidenceLedger) -> RoleOutput:
    findings: list[Finding] = []
    for f in out.get("findings", []):
        ids = [i for i in f.get("evidence_ids", []) if ledger.has_citation(i)]
        if not ids and f.get("type") in ("fact", "source_opinion"):
            continue  # a fact without valid evidence is dropped
        findings.append(
            Finding(
                statement=str(f.get("statement", "")),
                type=FindingType(f.get("type", "inference")),
                evidence_ids=ids,
                confidence=float(f.get("confidence", 0.5)),
                caveat=f.get("caveat"),
            )
        )
    return RoleOutput(
        role="",
        findings=findings,
        open_questions=[str(q) for q in out.get("open_questions", [])],
        invalidated_if=[str(q) for q in out.get("invalidated_if", [])],
    )


def run_role(
    role: str,
    provider: LLMProvider,
    ledger: EvidenceLedger,
    calcs: dict[str, CalculationResult],
    scenarios: list[Scenario],
    horizon: str,
) -> RoleOutput:
    context = {
        "horizon": horizon,
        "calculations": {k: c.outputs for k, c in calcs.items()},
        "calculation_warnings": {k: c.warnings for k, c in calcs.items()},
        "scenarios": [s.model_dump(mode="json") for s in scenarios],
    }
    try:
        raw = provider.structured(ROLE_TASKS[role], ledger.all(), ROLE_SCHEMA, context)
        out = _validate_citations(raw, ledger)
        out.role, out.model = role, f"{provider.name}:{provider.model}"
        return out
    except LLMError:
        out = _deterministic_role(role, ledger, calcs, scenarios)
        out.role, out.model = role, "deterministic"
        return out
    except Exception as exc:  # schema/parse failure after repair: discard model result
        out = _deterministic_role(role, ledger, calcs, scenarios)
        out.role, out.model = role, "deterministic"
        out.open_questions.append(f"model output discarded: {type(exc).__name__}")
        return out


# ------------------------------------------------------------------ fallbacks
def _pct(x: object) -> str:
    return f"{x:.1%}" if isinstance(x, (int, float)) else "n/a"


def _num(x: object) -> str:
    if not isinstance(x, (int, float)):
        return "n/a"
    return f"{x:,.2f}" if abs(x) < 1e4 else f"{x:,.0f}"


def _f(
    stmt: str,
    ids: list[str],
    t: FindingType = FindingType.MODEL_OUTPUT,
    conf: float = 0.7,
    caveat: str | None = None,
) -> Finding:
    return Finding(statement=stmt, type=t, evidence_ids=ids, confidence=conf, caveat=caveat)


def _deterministic_role(
    role: str,
    ledger: EvidenceLedger,
    calcs: dict[str, CalculationResult],
    scenarios: list[Scenario],
) -> RoleOutput:
    fund = calcs.get("fundamentals")
    tech = calcs.get("technicals")
    val = calcs.get("valuation")
    findings: list[Finding] = []
    questions: list[str] = []
    invalid: list[str] = []

    if role == "fundamentals" and fund:
        o = fund.outputs
        ids = fund.evidence_ids
        findings.append(
            _f(
                f"Revenue growth YoY (filing basis): {_pct(o['revenue_growth_yoy'])}; "
                f"net margin {_pct(o['net_margin'])}; operating margin "
                f"{_pct(o['operating_margin'])}.",
                ids,
            )
        )
        findings.append(
            _f(
                f"Free cash flow {_num(o['free_cash_flow'])} (FCF margin "
                f"{_pct(o['fcf_margin'])}, FCF/NI {_num(o['cash_conversion_fcf_to_ni'])}); "
                f"net debt {_num(o['net_debt'])}; ROE {_pct(o['roe'])}.",
                ids,
            )
        )
        for flag in o.get("red_flags", []):
            findings.append(_f(f"Red flag: {flag}.", ids, conf=0.8))
        questions += [f"Missing input: {w}" for w in fund.warnings if w.startswith("missing")][:5]
        invalid.append(
            "Revenue growth or margins fall materially below the base-case drivers "
            "for two consecutive quarters."
        )

    elif role == "valuation" and val:
        o = val.outputs
        ids = val.evidence_ids
        grid = o.get("dcf_per_share_by_growth")
        if isinstance(grid, dict):
            findings.append(
                _f(
                    "DCF per share at 0/5/10/15% FCF growth "
                    f"(r={o['discount_rate']:.1%}, g_T={o['terminal_growth']:.1%}): "
                    + ", ".join(f"{_num(v)}" for v in grid.values())
                    + ".",
                    ids,
                )
            )
        ig = o.get("implied_fcf_growth_at_market_price")
        if isinstance(ig, float):
            findings.append(
                _f(
                    f"Reverse DCF: market price implies about {ig:.1%}/yr FCF growth "
                    "over 5 years under the same assumptions.",
                    ids,
                    t=FindingType.INFERENCE,
                    conf=0.6,
                    caveat="Sensitive to discount rate and terminal growth.",
                )
            )
        if "forward_pe_at_price" in o:
            findings.append(
                _f(
                    f"Forward P/E at current price: {_num(o['forward_pe_at_price'])}x.",
                    ids,
                    t=FindingType.FACT,
                    conf=0.7,
                )
            )
        questions += val.warnings
        invalid.append("Forward EPS estimates revised down by more than 10%.")

    elif role == "technical" and tech:
        o = tech.outputs
        ids = tech.evidence_ids
        findings.append(
            _f(
                f"Trend: {o['trend_label']}; price vs 50d SMA {_pct(o['price_vs_sma50'])}, "
                f"vs 200d SMA {_pct(o['price_vs_sma200'])}; RSI(14) {_num(o['rsi_14'])}.",
                ids,
                caveat="Indicators describe past price behaviour, not future direction.",
            )
        )
        findings.append(
            _f(
                f"1y max drawdown {_pct(o['max_drawdown_1y'])}; annualized volatility "
                f"{_pct(o['annualized_volatility'])}; 1y range {_num(o['support_1y_low'])}–"
                f"{_num(o['resistance_1y_high'])}.",
                ids,
            )
        )
        questions += tech.warnings

    elif role == "catalyst_risk":
        ids = [r.id for r in ledger.by_field("price_intraday")]
        findings.append(
            _f(
                "Generic catalysts: quarterly earnings and guidance, product/segment "
                "announcements, capital-return changes, regulatory or macro shifts.",
                ids,
                t=FindingType.INFERENCE,
                conf=0.4,
                caveat="Company-specific catalysts require news/IR sources not in ledger.",
            )
        )
        for s in scenarios:
            if s.name == "Bear":
                invalid += s.what_must_be_true
        questions.append(
            "No news/IR adapter results in ledger; company-specific catalysts unverified."
        )

    elif role == "forecast_auditor":
        mean = ledger.best("analyst_target_mean")
        if mean:
            n = ledger.value("analyst_count")
            findings.append(
                _f(
                    f"Yahoo consensus mean target {_num(mean.value)} as of {mean.as_of} "
                    f"from {int(n) if n else 'an undisclosed number of'} analysts; "
                    "methodology not disclosed; likely duplicated by other aggregators.",
                    [mean.id],
                    t=FindingType.SOURCE_OPINION,
                    conf=0.5,
                )
            )
        for r in ledger.by_field("cross_check_link"):
            findings.append(
                _f(
                    f"{r.source_name}: no permitted structured feed; values not retrieved.",
                    [r.id],
                    t=FindingType.FACT,
                    conf=0.9,
                )
            )
        questions.append(
            "Analyst target ages are unknown individually; consensus date is the page date."
        )

    elif role == "skeptic":
        ids = fund.evidence_ids[:3] if fund else []
        bear = next((s for s in scenarios if s.name == "Bear"), None)
        if bear and bear.implied_price_mid is not None:
            findings.append(
                _f(
                    f"Bear case implies about {_num(bear.implied_price_mid)} if growth slows "
                    f"to {bear.drivers.revenue_growth:+.1%}, margins compress "
                    f"{-0.03:.0%} and the multiple de-rates ~20%.",
                    ids,
                )
            )
        findings.append(
            _f(
                "Scenario drivers are extrapolations from one or two fiscal years and "
                "current multiples; they carry no information about regime changes.",
                ids,
                t=FindingType.INFERENCE,
                conf=0.6,
            )
        )
        questions.append("What would a competitor or regulator need to do to break the base case?")
        invalid.append("A restatement, going-concern language, or auditor change in a filing.")

    return RoleOutput(
        role=role, findings=findings, open_questions=questions, invalidated_if=invalid
    )
