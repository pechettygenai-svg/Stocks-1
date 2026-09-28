import math

import pytest

from stock_forecaster.analytics.scenarios import ScenarioInputs, build_scenarios
from stock_forecaster.analytics.technicals import (
    annualized_volatility,
    max_drawdown,
    rsi,
    sma,
    technical_summary,
)
from stock_forecaster.analytics.valuation import dcf_per_share, reverse_dcf_growth


def test_sma_and_short_series():
    assert sma([1, 2, 3, 4], 2) == 3.5
    assert sma([1, 2], 5) is None


def test_rsi_bounds_and_monotonic():
    up = [float(i) for i in range(1, 40)]
    assert rsi(up) == 100.0
    down = up[::-1]
    assert rsi(down) == pytest.approx(0.0)
    assert rsi([1.0] * 5) is None


def test_drawdown_and_vol():
    assert max_drawdown([10, 12, 6, 9]) == pytest.approx(-0.5)
    assert annualized_volatility([100.0] * 30) == pytest.approx(0.0)
    assert annualized_volatility([1.0, 2.0]) is None


def test_technical_summary_warns_on_short_history():
    closes = [100 + math.sin(i / 5) * 5 for i in range(60)]
    res = technical_summary(closes, [1e6] * 60, ["ev-001"])
    assert res.outputs["sma_200"] is None
    assert any("200-day" in w for w in res.warnings)
    assert res.evidence_ids == ["ev-001"]


def test_dcf_round_trip_reverse():
    args = dict(years=5, discount_rate=0.09, terminal_growth=0.025, net_debt=50.0, shares=100.0)
    price = dcf_per_share(fcf0=100.0, growth=0.08, **args)
    assert price is not None and price > 0
    implied = reverse_dcf_growth(price=price, fcf0=100.0, **args)
    assert implied == pytest.approx(0.08, abs=1e-6)


def test_dcf_rejects_bad_inputs():
    assert dcf_per_share(1, 0.1, 5, 0.02, 0.03, 0, 10) is None  # r <= g
    assert reverse_dcf_growth(10, -5, 5, 0.09, 0.02, 0, 10) is None  # negative FCF


def _inputs(**over):
    base = dict(
        price=100.0,
        revenue=1000.0,
        net_margin=0.20,
        shares=10.0,
        eps=20.0,
        revenue_growth_hist=0.10,
        pe_current=5.0,
        ps_current=1.0,
        horizon_years=1.0,
    )
    base.update(over)
    return ScenarioInputs(**base)


def test_scenarios_ordered_and_probabilities_sum():
    scen, calc = build_scenarios(_inputs())
    names = [s.name for s in scen]
    assert names == ["Bear", "Base", "Bull"]
    mids = [s.implied_price_mid for s in scen]
    assert mids[0] < mids[1] < mids[2]
    assert sum(s.probability for s in scen) == pytest.approx(1.0)
    assert calc.outputs["probabilities_are_assumptions"] is True
    # base: rev 1100 * 0.2 = 220 NI / 10 shares = 22 EPS * 5x (clipped to >=8x) = 176
    assert scen[1].implied_price_mid == pytest.approx(22 * 8.0)


def test_scenarios_switch_to_sales_mode_when_unprofitable():
    scen, calc = build_scenarios(_inputs(net_margin=-0.1, eps=-1.0))
    assert calc.outputs["mode"] == "sales"
    assert any("not profitable" in w for w in calc.warnings)
    assert all(s.implied_price_mid is not None for s in scen)


def test_scenarios_missing_inputs_do_not_substitute_zero():
    scen, _ = build_scenarios(_inputs(revenue=None, shares=None, eps=None))
    assert all(s.implied_price_mid is None for s in scen)
    assert all(s.warnings for s in scen)
