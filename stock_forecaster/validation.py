"""Deterministic checks run before any model interpretation."""

from __future__ import annotations

import re
from datetime import date

from .ledger import EvidenceLedger
from .models import EvidenceRecord, ForecastComparison, RetrievalStatus, SourceType

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


def forecast_panel(
    ledger: EvidenceLedger, horizon: str, today: date | None = None
) -> list[ForecastComparison]:
    """Normalize every external forecast into one comparison row, then flag
    duplicates (same value + date across aggregators), stale rows and outliers.
    Rows are never averaged."""
    today = today or date.today()
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
                value_low=_num(lo),
                value_mean=_num(mean),
                value_high=_num(hi),
                as_of=mean.as_of,
                analyst_count=int(count) if count else None,
                method_disclosed=False,
                weight="context",
                url=mean.source_url,
                evidence_ids=[r.id for r in (lo, mean, hi) if r],
            )
        )

    # user-supplied transcriptions, grouped by forecast index
    groups: dict[str, dict[str, EvidenceRecord]] = {}
    for r in ledger.all():
        if r.field and r.field.startswith("user_forecast_"):
            _, _, idx, kind = r.field.split("_", 3)
            groups.setdefault(idx, {})[kind] = r
    for idx in sorted(groups):
        g = groups[idx]
        any_r = next(iter(g.values()))
        n = _analyst_count_from_notes(any_r.notes)
        panel.append(
            ForecastComparison(
                source=any_r.source_name,
                forecast_type=any_r.definition or "forecast",
                horizon=any_r.period or horizon,
                value_low=_num(g.get("low")),
                value_mean=_num(g.get("mean")),
                value_high=_num(g.get("high")),
                as_of=any_r.as_of,
                analyst_count=n,
                method_disclosed="method_disclosed=True" in (any_r.notes or ""),
                weight="low",
                status=RetrievalStatus.SNIPPET_ONLY,
                url=any_r.source_url,
                evidence_ids=[r.id for r in g.values()],
                notes=["user-supplied transcription; not fetched or verified by this tool"],
            )
        )

    linked = {p.source.split(" (")[0].lower() for p in panel}
    for r in ledger.by_field("cross_check_link"):
        if r.source_name.split(" (")[0].lower() in linked:
            continue  # user supplied a value for this source
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

    _flag_duplicates_stale_outliers(panel, ledger.value("price_intraday"), today)
    return panel


def _num(r: EvidenceRecord | None) -> float | None:
    return float(r.value) if r is not None and isinstance(r.value, (int, float)) else None


def _analyst_count_from_notes(notes: str | None) -> int | None:
    m = re.search(r"analysts=(\d+)", notes or "")
    return int(m.group(1)) if m else None


def _flag_duplicates_stale_outliers(
    panel: list[ForecastComparison], price: float | None, today: date
) -> None:
    valued = [p for p in panel if p.value_mean is not None]
    for i, p in enumerate(valued):
        for q in valued[:i]:
            same_val = abs((p.value_mean or 0) - (q.value_mean or 0)) <= 0.005 * abs(
                q.value_mean or 1
            )
            same_range = (p.value_low, p.value_high) == (q.value_low, q.value_high)
            if same_val and (same_range or p.analyst_count == q.analyst_count):
                p.duplicate_of = q.source
                p.weight = "duplicate"
                p.notes.append(
                    f"identical mean{' and range' if same_range else ''} to {q.source}; "
                    "likely the same underlying consensus feed, counted once"
                )
                break
        if p.as_of and (today - p.as_of).days > 45:
            p.stale = True
            p.notes.append(f"{(today - p.as_of).days} days old")
    if price and len(valued) >= 2:
        means = sorted(m.value_mean or 0 for m in valued)
        median = means[len(means) // 2]
        for p in valued:
            if p.value_mean and abs(p.value_mean - median) / median > 0.25:
                p.outlier = True
                p.notes.append(f"mean differs from panel median {median:,.2f} by >25%")
