"""Bull/base/bear scenario engine built from explicit drivers.

Probabilities are neutral starting assumptions (25/50/25), not forecasts.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import CalculationResult, Scenario, ScenarioDrivers

FORMULA_VERSION = "scenarios-1.0"
DEFAULT_PROBS = {"Bear": 0.25, "Base": 0.50, "Bull": 0.25}


@dataclass
class ScenarioInputs:
    price: float
    revenue: float | None
    net_margin: float | None
    shares: float | None
    eps: float | None
    revenue_growth_hist: float | None
    pe_current: float | None
    ps_current: float | None
    horizon_years: float


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def build_drivers(inp: ScenarioInputs) -> tuple[list[ScenarioDrivers], str, list[str]]:
    """Derive neutral driver sets from observed data. Returns drivers, valuation
    mode ('earnings' | 'sales') and warnings."""
    warnings: list[str] = []
    g0 = inp.revenue_growth_hist
    if g0 is None:
        g0 = 0.05
        warnings.append("no historical revenue growth; base growth assumed 5%")
    g0 = _clip(g0, -0.10, 0.30)

    profitable = (
        inp.net_margin is not None and inp.net_margin > 0 and inp.eps is not None and inp.eps > 0
    )
    if profitable:
        mode = "earnings"
        m0 = inp.net_margin or 0.0
        mult = inp.pe_current if inp.pe_current and inp.pe_current > 0 else 15.0
        if inp.pe_current is None:
            warnings.append("no current P/E; exit multiple assumed 15x")
        mult = _clip(mult, 8.0, 45.0)
    else:
        mode = "sales"
        m0 = inp.net_margin if inp.net_margin is not None else 0.0
        mult = inp.ps_current if inp.ps_current and inp.ps_current > 0 else 3.0
        mult = _clip(mult, 0.5, 20.0)
        warnings.append(
            "company not profitable on latest data; scenarios use price/sales exit multiple"
        )

    drivers = [
        ScenarioDrivers(
            label="Bear",
            revenue_growth=max(g0 - 0.07, -0.15),
            operating_margin=m0 - 0.03,
            share_change=0.01,
            exit_multiple=mult * 0.80,
        ),
        ScenarioDrivers(
            label="Base",
            revenue_growth=g0,
            operating_margin=m0,
            share_change=0.0,
            exit_multiple=mult,
        ),
        ScenarioDrivers(
            label="Bull",
            revenue_growth=g0 + 0.05,
            operating_margin=m0 + 0.02,
            share_change=-0.01,
            exit_multiple=mult * 1.15,
        ),
    ]
    return drivers, mode, warnings


def implied_price(d: ScenarioDrivers, inp: ScenarioInputs, mode: str) -> float | None:
    T = inp.horizon_years
    if mode == "earnings":
        if inp.revenue and inp.shares:
            rev_t = inp.revenue * (1 + d.revenue_growth) ** T
            ni_t = rev_t * d.operating_margin
            sh_t = inp.shares * (1 + d.share_change) ** T
            if sh_t <= 0 or ni_t <= 0:
                return None
            return float((ni_t / sh_t) * d.exit_multiple)
        if inp.eps:
            eps_t = inp.eps * (1 + d.revenue_growth) ** T / (1 + d.share_change) ** T
            return eps_t * d.exit_multiple if eps_t > 0 else None
        return None
    if inp.revenue and inp.shares:
        rev_t = inp.revenue * (1 + d.revenue_growth) ** T
        sh_t = inp.shares * (1 + d.share_change) ** T
        return (rev_t / sh_t) * d.exit_multiple if sh_t > 0 else None
    return None


def build_scenarios(inp: ScenarioInputs) -> tuple[list[Scenario], CalculationResult]:
    drivers, mode, warnings = build_drivers(inp)
    scenarios: list[Scenario] = []
    for d in drivers:
        mid = implied_price(d, inp, mode)
        low = mid * 0.9 if mid is not None else None
        high = mid * 1.1 if mid is not None else None
        must = [
            f"Revenue compounds at about {d.revenue_growth:+.1%}/yr over {inp.horizon_years:g} years",
            f"Net margin near {d.operating_margin:.1%}",
            f"Share count changes about {d.share_change:+.1%}/yr",
            f"Market pays about {d.exit_multiple:.1f}x {'earnings' if mode == 'earnings' else 'sales'} "
            "at horizon",
        ]
        scenarios.append(
            Scenario(
                name=d.label,
                drivers=d,
                implied_price_low=low,
                implied_price_mid=mid,
                implied_price_high=high,
                probability=DEFAULT_PROBS[d.label],
                what_must_be_true=must,
                warnings=[] if mid is not None else ["implied value not computable from inputs"],
            )
        )

    weighted: float | None = None
    mids = [s.implied_price_mid for s in scenarios]
    if all(m is not None for m in mids):
        weighted = sum((m or 0.0) * s.probability for m, s in zip(mids, scenarios, strict=True))

    base = drivers[1]
    sensitivity: dict[str, dict[str, float | None]] = {}
    for dg in (-0.05, 0.0, 0.05):
        row: dict[str, float | None] = {}
        for dm in (0.8, 1.0, 1.2):
            dd = base.model_copy(
                update={
                    "revenue_growth": base.revenue_growth + dg,
                    "exit_multiple": base.exit_multiple * dm,
                }
            )
            row[f"multiple_x{dm:.1f}"] = implied_price(dd, inp, mode)
        sensitivity[f"growth_{dg:+.0%}"] = row

    calc = CalculationResult(
        name="scenarios",
        formula_version=FORMULA_VERSION,
        inputs={
            "price": inp.price,
            "revenue": inp.revenue,
            "net_margin": inp.net_margin,
            "shares": inp.shares,
            "eps": inp.eps,
            "revenue_growth_hist": inp.revenue_growth_hist,
            "pe_current": inp.pe_current,
            "ps_current": inp.ps_current,
            "horizon_years": inp.horizon_years,
            "mode": mode,
        },
        outputs={
            "mode": mode,
            "probability_weighted_mid": weighted,
            "probabilities_are_assumptions": True,
            "sensitivity_base": sensitivity,
            "upside_downside_vs_price": {
                s.name: (s.implied_price_mid / inp.price - 1) if s.implied_price_mid else None
                for s in scenarios
            },
        },
        warnings=warnings,
    )
    return scenarios, calc
