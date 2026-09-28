"""Fundamental ratios recomputed from ledger values. Missing inputs are
reported as warnings, never substituted with zero."""

from __future__ import annotations

from ..ledger import EvidenceLedger
from ..models import CalculationResult

FORMULA_VERSION = "fundamentals-1.0"


def _ratio(num: float | None, den: float | None) -> float | None:
    if num is None or den is None or den == 0:
        return None
    return num / den


def fundamental_summary(ledger: EvidenceLedger) -> CalculationResult:
    L = ledger
    used = [
        "revenue_fy",
        "revenue_fy_prior",
        "operating_income_fy",
        "net_income_fy",
        "net_income_fy_prior",
        "operating_cash_flow_fy",
        "capex_fy",
        "diluted_shares_fy",
        "diluted_shares_fy_prior",
        "cash",
        "long_term_debt",
        "total_equity",
        "total_assets",
        "market_cap",
        "price_intraday",
        "eps_ttm",
        "eps_forward",
        "revenue_ttm",
        "free_cash_flow_ttm",
        "net_interest_income_fy",
        "provision_credit_losses_fy",
        "loans_net",
        "deposits",
        "eps_diluted_fy",
    ]
    v = {f: L.value(f) for f in used}
    ev_ids = [r.id for f in used if (r := L.best(f)) is not None]
    warnings: list[str] = [f"missing input: {f}" for f in used if v[f] is None]

    rev, rev_p = v["revenue_fy"], v["revenue_fy_prior"]
    ni, ni_p = v["net_income_fy"], v["net_income_fy_prior"]
    ocf, capex = v["operating_cash_flow_fy"], v["capex_fy"]
    fcf = (ocf - abs(capex)) if ocf is not None and capex is not None else None
    if fcf is None and v["free_cash_flow_ttm"] is not None:
        fcf = v["free_cash_flow_ttm"]
        warnings.append("FCF taken from market-data provider (TTM), not recomputed from filing")
    debt, cash = v["long_term_debt"], v["cash"]
    net_debt = (debt - cash) if debt is not None and cash is not None else None
    shares, shares_p = v["diluted_shares_fy"], v["diluted_shares_fy_prior"]
    mcap = v["market_cap"]
    price = v["price_intraday"]

    outputs: dict[str, object] = {
        "revenue_growth_yoy": _ratio(rev - rev_p, abs(rev_p))
        if rev is not None and rev_p
        else None,
        "net_income_growth_yoy": _ratio(ni - ni_p, abs(ni_p)) if ni is not None and ni_p else None,
        "operating_margin": _ratio(v["operating_income_fy"], rev),
        "net_margin": _ratio(ni, rev),
        "free_cash_flow": fcf,
        "fcf_margin": _ratio(fcf, rev),
        "cash_conversion_fcf_to_ni": _ratio(fcf, ni),
        "net_debt": net_debt,
        "net_debt_to_equity": _ratio(net_debt, v["total_equity"]),
        "roe": _ratio(ni, v["total_equity"]),
        "roa": _ratio(ni, v["total_assets"]),
        "share_count_change_yoy": _ratio(shares - shares_p, shares_p)
        if shares is not None and shares_p
        else None,
        "fcf_yield": _ratio(fcf, mcap),
        "earnings_yield_ttm": _ratio(v["eps_ttm"], price),
        "pe_ttm_recomputed": _ratio(price, v["eps_ttm"]),
        "pe_forward_recomputed": _ratio(price, v["eps_forward"]),
        "price_to_book_recomputed": _ratio(mcap, v["total_equity"]),
        "market_cap_to_revenue_fy": _ratio(mcap, rev),
        # bank-specific (None for non-banks)
        "nii_to_revenue": _ratio(v["net_interest_income_fy"], rev),
        "provision_to_loans": _ratio(v["provision_credit_losses_fy"], v["loans_net"]),
        "loans_to_deposits": _ratio(v["loans_net"], v["deposits"]),
        "equity_to_assets": _ratio(v["total_equity"], v["total_assets"]),
    }

    red_flags: list[str] = []
    if ni is not None and ni < 0:
        red_flags.append("negative net income (latest FY)")
    if fcf is not None and fcf < 0:
        red_flags.append("negative free cash flow")
    sc = outputs["share_count_change_yoy"]
    if isinstance(sc, float) and sc > 0.03:
        red_flags.append(f"diluted share count up {sc:.1%} YoY")
    cc = outputs["cash_conversion_fcf_to_ni"]
    if isinstance(cc, float) and ni and ni > 0 and cc < 0.6:
        red_flags.append(f"weak cash conversion: FCF/NI = {cc:.2f}")
    outputs["red_flags"] = red_flags

    return CalculationResult(
        name="fundamentals",
        formula_version=FORMULA_VERSION,
        inputs={k: val for k, val in v.items() if val is not None},
        outputs=outputs,
        warnings=warnings,
        evidence_ids=ev_ids,
    )
