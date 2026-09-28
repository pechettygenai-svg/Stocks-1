"""Deterministic checks run before any model interpretation."""

from __future__ import annotations

from datetime import date

from .ledger import EvidenceLedger
from .models import ForecastComparison, RetrievalStatus, SourceType

STALE_MARKET_DAYS = 5
STALE_FILING_DAYS = 400


def validate(ledger: EvidenceLedger, today: date | None = None) -> list[str]:
    today = today or date.today()
    warnings: list[str] = []

    price = ledger.value("price_intraday")
    mcap = ledger.value("market_cap")
    shares = ledger.value("shares_outstanding")
    if price is not None and mcap is not None and shares:
        implied = price * shares
        diff = abs(implied - mcap) / max(mcap, 1)
        if diff > 0.10:
            warnings.append(
                f"market cap sanity: price x shares = {implied:,.0f} differs from reported "
                f"market cap {mcap:,.0f} by {diff:.0%}"
            )
    lo, hi = ledger.value("week52_low"), ledger.value("week52_high")
    if (
        price is not None
        and lo is not None
        and hi is not None
        and not (lo * 0.98 <= price <= hi * 1.02)
    ):
        warnings.append(f"price {price} outside 52-week range [{lo}, {hi}]")

    # Cross-source revenue / net income comparison (filing vs market data)
    for fil, md, label in (
        ("revenue_fy", "revenue_ttm", "revenue"),
        ("net_income_fy", "net_income_ttm", "net income"),
    ):
        a, b = ledger.value(fil), ledger.value(md)
        if a is not None and b is not None and a != 0:
            diff = abs(a - b) / abs(a)
            if diff > 0.15:
                warnings.append(
                    f"{label}: filing FY {a:,.0f} vs provider TTM {b:,.0f} differ by {diff:.0%} "
                    "(period/definition difference, both retained)"
                )

    # Currency consistency
    currencies = {r.currency for r in ledger.all() if r.currency}
    if len(currencies) > 1:
        warnings.append(f"multiple currencies in ledger: {sorted(currencies)}")

    # Staleness
    for r in ledger.all():
        if r.as_of is None or r.retrieval_status == RetrievalStatus.UNAVAILABLE:
            continue
        age = (today - r.as_of).days
        if (
            r.source_type in (SourceType.MARKET_DATA, SourceType.FORECAST)
            and age > STALE_MARKET_DAYS
        ):
            warnings.append(f"{r.id} ({r.field}) is {age} days old — treat as stale")
        elif (
            r.source_type == SourceType.FILING
            and age > STALE_FILING_DAYS
            and not (r.field or "").endswith("_prior")
        ):
            warnings.append(f"{r.id} ({r.field}) fiscal period ended {age} days ago")

    # Intra-field conflicts
    for fld in {r.field for r in ledger.all() if r.field}:
        for id_a, id_b, diff in ledger.conflicts(fld):
            warnings.append(f"conflict on {fld}: {id_a} vs {id_b} differ by {diff:.0%}")

    ni = ledger.value("net_income_fy")
    if ni is not None and ni < 0:
        warnings.append("negative net income in latest fiscal year")
    for r in ledger.all():
        if r.retrieval_status == RetrievalStatus.UNAVAILABLE:
            warnings.append(f"source unavailable: {r.source_name} — {r.notes}")
    return warnings


def forecast_panel(ledger: EvidenceLedger, horizon: str) -> list[ForecastComparison]:
    panel: list[ForecastComparison] = []
    lo, mean, hi = (
        ledger.best(f) for f in ("analyst_target_low", "analyst_target_mean", "analyst_target_high")
    )
    count = ledger.value("analyst_count")
    if mean is not None:
        panel.append(
            ForecastComparison(
                source="Yahoo Finance (analyst consensus)",
                forecast_type="analyst price target (consensus)",
                horizon="12m",
                value_low=float(lo.value) if lo and isinstance(lo.value, float) else None,
                value_mean=float(mean.value) if isinstance(mean.value, float) else None,
                value_high=float(hi.value) if hi and isinstance(hi.value, float) else None,
                as_of=mean.as_of,
                analyst_count=int(count) if count else None,
                method_disclosed=False,
                weight="context",
                url=mean.source_url,
                evidence_ids=[r.id for r in (lo, mean, hi) if r],
            )
        )
    for r in ledger.by_field("cross_check_link"):
        panel.append(
            ForecastComparison(
                source=r.source_name,
                forecast_type="unavailable (no permitted structured feed)",
                horizon=horizon,
                status=RetrievalStatus.UNAVAILABLE,
                weight="none",
                url=r.source_url,
                evidence_ids=[r.id],
            )
        )
    return panel
