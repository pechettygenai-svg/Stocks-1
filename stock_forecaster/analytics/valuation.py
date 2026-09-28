"""Valuation math: DCF, reverse DCF and multiple-based values."""

from __future__ import annotations

from ..models import CalculationResult

FORMULA_VERSION = "valuation-1.0"


def dcf_per_share(
    fcf0: float,
    growth: float,
    years: int,
    discount_rate: float,
    terminal_growth: float,
    net_debt: float,
    shares: float,
) -> float | None:
    if shares <= 0 or discount_rate <= terminal_growth:
        return None
    pv = 0.0
    fcf = fcf0
    for t in range(1, years + 1):
        fcf *= 1 + growth
        pv += fcf / (1 + discount_rate) ** t
    terminal = fcf * (1 + terminal_growth) / (discount_rate - terminal_growth)
    pv += terminal / (1 + discount_rate) ** years
    return (pv - net_debt) / shares


def reverse_dcf_growth(
    price: float,
    fcf0: float,
    years: int,
    discount_rate: float,
    terminal_growth: float,
    net_debt: float,
    shares: float,
    lo: float = -0.5,
    hi: float = 1.0,
) -> float | None:
    """Implied FCF growth that makes the DCF equal the market price (bisection)."""
    if fcf0 <= 0 or shares <= 0:
        return None
    f_lo = dcf_per_share(fcf0, lo, years, discount_rate, terminal_growth, net_debt, shares)
    f_hi = dcf_per_share(fcf0, hi, years, discount_rate, terminal_growth, net_debt, shares)
    if f_lo is None or f_hi is None or (f_lo - price) * (f_hi - price) > 0:
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        f_mid = dcf_per_share(fcf0, mid, years, discount_rate, terminal_growth, net_debt, shares)
        if f_mid is None:
            return None
        if (f_mid - price) * (f_lo - price) <= 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


def valuation_summary(
    price: float | None,
    fcf: float | None,
    net_debt: float | None,
    shares: float | None,
    eps_forward: float | None,
    evidence_ids: list[str],
    discount_rate: float = 0.09,
    terminal_growth: float = 0.025,
    years: int = 5,
) -> CalculationResult:
    warnings: list[str] = []
    outputs: dict[str, object] = {
        "discount_rate": discount_rate,
        "terminal_growth": terminal_growth,
    }
    if fcf is None or shares is None or net_debt is None:
        warnings.append("DCF skipped: requires FCF, net debt and diluted shares")
    elif fcf <= 0:
        warnings.append("DCF not meaningful: negative/zero free cash flow")
    else:
        grid = {}
        for g in (0.0, 0.05, 0.10, 0.15):
            grid[f"fcf_growth_{int(g * 100)}pct"] = dcf_per_share(
                fcf, g, years, discount_rate, terminal_growth, net_debt, shares
            )
        outputs["dcf_per_share_by_growth"] = grid
        if price:
            outputs["implied_fcf_growth_at_market_price"] = reverse_dcf_growth(
                price, fcf, years, discount_rate, terminal_growth, net_debt, shares
            )
    if price and eps_forward and eps_forward > 0:
        outputs["forward_pe_at_price"] = price / eps_forward
        outputs["price_at_forward_pe"] = {f"{m}x": m * eps_forward for m in (12, 15, 20, 25, 30)}
    else:
        warnings.append("forward multiple grid skipped: forward EPS missing or non-positive")
    return CalculationResult(
        name="valuation",
        formula_version=FORMULA_VERSION,
        inputs={
            "price": price,
            "fcf": fcf,
            "net_debt": net_debt,
            "shares": shares,
            "eps_forward": eps_forward,
            "years": years,
        },
        outputs=outputs,
        warnings=warnings,
        evidence_ids=evidence_ids,
    )
