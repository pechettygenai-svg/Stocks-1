"""Deterministic technical indicators. Context, not predictions."""

from __future__ import annotations

import math

from ..models import CalculationResult

FORMULA_VERSION = "technicals-1.0"


def sma(values: list[float], window: int) -> float | None:
    if len(values) < window or window <= 0:
        return None
    return sum(values[-window:]) / window


def ema(values: list[float], window: int) -> float | None:
    if len(values) < window or window <= 0:
        return None
    k = 2 / (window + 1)
    e = sum(values[:window]) / window
    for v in values[window:]:
        e = v * k + e * (1 - k)
    return e


def rsi(values: list[float], window: int = 14) -> float | None:
    if len(values) < window + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for prev, cur in zip(values[:-1], values[1:], strict=True):
        d = cur - prev
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains[:window]) / window
    avg_loss = sum(losses[:window]) / window
    for g, lo in zip(gains[window:], losses[window:], strict=True):
        avg_gain = (avg_gain * (window - 1) + g) / window
        avg_loss = (avg_loss * (window - 1) + lo) / window
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def max_drawdown(values: list[float]) -> float | None:
    if not values:
        return None
    peak = values[0]
    mdd = 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1)
    return mdd


def annualized_volatility(values: list[float], trading_days: int = 252) -> float | None:
    if len(values) < 3:
        return None
    rets = [
        math.log(b / a) for a, b in zip(values[:-1], values[1:], strict=True) if a > 0 and b > 0
    ]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(trading_days)


def total_return(values: list[float]) -> float | None:
    if len(values) < 2 or values[0] <= 0:
        return None
    return values[-1] / values[0] - 1


def volume_ratio(volumes: list[float], short: int = 10, long: int = 50) -> float | None:
    s, lg = sma(volumes, short), sma(volumes, long)
    if s is None or lg is None or lg == 0:
        return None
    return s / lg


def technical_summary(
    closes: list[float], volumes: list[float], evidence_ids: list[str]
) -> CalculationResult:
    warnings: list[str] = []
    if len(closes) < 200:
        warnings.append(
            f"only {len(closes)} sessions of history; 200-day SMA unavailable/less reliable"
        )
    last = closes[-1] if closes else None
    s50, s200 = sma(closes, 50), sma(closes, 200)
    outputs = {
        "last_close": last,
        "sma_50": s50,
        "sma_200": s200,
        "ema_20": ema(closes, 20),
        "rsi_14": rsi(closes, 14),
        "max_drawdown_1y": max_drawdown(closes),
        "annualized_volatility": annualized_volatility(closes),
        "total_return_period": total_return(closes),
        "volume_ratio_10_50": volume_ratio(volumes),
        "price_vs_sma50": (last / s50 - 1) if last and s50 else None,
        "price_vs_sma200": (last / s200 - 1) if last and s200 else None,
        "trend_label": _trend_label(last, s50, s200),
        "support_1y_low": min(closes) if closes else None,
        "resistance_1y_high": max(closes) if closes else None,
    }
    return CalculationResult(
        name="technicals",
        formula_version=FORMULA_VERSION,
        inputs={"sessions": len(closes)},
        outputs=outputs,
        warnings=warnings,
        evidence_ids=evidence_ids,
    )


def _trend_label(last: float | None, s50: float | None, s200: float | None) -> str:
    if last is None or s50 is None:
        return "insufficient data"
    if s200 is None:
        return "above 50d SMA" if last > s50 else "below 50d SMA"
    if last > s50 > s200:
        return "uptrend (price > 50d > 200d)"
    if last < s50 < s200:
        return "downtrend (price < 50d < 200d)"
    return "mixed / transitional"
