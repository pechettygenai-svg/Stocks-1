"""Watchlist change detection.

A watchlist re-runs the normal pipeline per ticker and compares the new run with
the last baseline. Alerts fire only for *data* changes (new or restated filing
values, a moved consensus, a source becoming (un)available) or *thesis* changes
(scenario mode, price leaving the previous run's bear-low..bull-high range, red
flags, gate failures, blocking review findings). A routine re-run where only the
price drifted inside the previously modeled range produces no alert.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any, Literal

from pydantic import BaseModel, Field

from .models import (
    AnalysisRequest,
    AnalysisResult,
    Horizon,
    RetrievalStatus,
    ReviewSeverity,
    RunStatus,
    SourceType,
)
from .store import RunStore

# Relative move in a consensus number that counts as a change (2% = noise floor
# for daily-refreshed aggregator consensus).
CONSENSUS_REL = 0.02
RECOMMENDATION_ABS = 0.2
ANALYST_COUNT_ABS = 3
CONSENSUS_FIELDS = ("eps_forward", "recommendation_mean")
_EPS = 1e-9  # so a move of exactly the threshold counts despite float rounding


def _reaches(x: float, limit: float) -> bool:
    return x >= limit - _EPS


class Snapshot(BaseModel):
    run_id: str
    data: dict[str, Any] = Field(default_factory=dict)
    thesis: dict[str, Any] = Field(default_factory=dict)


class Change(BaseModel):
    kind: Literal["data", "thesis"]
    key: str
    before: Any = None
    after: Any = None
    message: str


def _flag_shape(s: str) -> str:
    """'diluted share count up 17.1% YoY' -> 'diluted share count up # YoY' so a flag
    whose number moves is not reported as a new flag."""
    return re.sub(r"-?\d[\d,.]*%?", "#", s)


def _price(result: AnalysisResult) -> float | None:
    for e in result.evidence:
        if e.field == "price_intraday" and isinstance(e.value, int | float):
            return float(e.value)
    return None


def scenario_range(result: AnalysisResult) -> list[float] | None:
    """[Bear low, Bull high]. Exit multiples track the current multiple, so this
    range moves with price; what matters is whether a later price leaves the
    range the previous run modeled."""
    sc = {s.name: s for s in result.scenarios}
    lo = sc["Bear"].implied_price_low if "Bear" in sc else None
    hi = sc["Bull"].implied_price_high if "Bull" in sc else None
    return [lo, hi] if lo is not None and hi is not None else None


def snapshot(result: AnalysisResult) -> Snapshot:
    data: dict[str, Any] = {}
    for e in result.evidence:
        if e.source_type == SourceType.FILING and e.field and e.value is not None:
            prev = data.get(f"filing:{e.field}")
            as_of = e.as_of.isoformat() if e.as_of else None
            if prev is None or (as_of or "") > (prev["as_of"] or ""):
                data[f"filing:{e.field}"] = {"value": e.value, "as_of": as_of}
        elif e.field in CONSENSUS_FIELDS and e.value is not None:
            data[f"consensus:{e.field}"] = {"value": e.value}
    for f in result.forecasts:
        data[f"forecast:{f.source}"] = {
            "status": f.status.value,
            "low": f.value_low,
            "mean": f.value_mean,
            "high": f.value_high,
            "analyst_count": f.analyst_count,
        }
    # Keyed by source + field, not the claim text: a continuing outage whose error
    # message changes is not an availability change.
    data["unavailable"] = sorted(
        {
            f"{e.source_name} ({e.field or e.source_type.value})"
            for e in result.evidence
            if e.retrieval_status == RetrievalStatus.UNAVAILABLE and e.field != "cross_check_link"
        }
    )

    calcs = {c.name: c for c in result.calculations}
    sc = calcs.get("scenarios")
    fund = calcs.get("fundamentals")
    thesis = {
        "scenario_mode": sc.outputs.get("mode") if sc else None,
        "price": _price(result),
        "scenario_range": scenario_range(result),
        "red_flags": sorted({_flag_shape(f) for f in (fund.outputs.get("red_flags") or [])})
        if fund
        else [],
        "quality_gate_failures": sorted(result.quality_gate_failures),
        "blocking_reviews": sorted(
            {r.check for r in result.reviews if r.severity == ReviewSeverity.BLOCKING}
        ),
    }
    return Snapshot(run_id=result.run_id, data=data, thesis=thesis)


def _rel(a: Any, b: Any) -> float:
    if not isinstance(a, int | float) or not isinstance(b, int | float):
        return 0.0 if a == b else float("inf")
    if a == b:
        return 0.0
    return abs(b - a) / abs(a) if a else float("inf")


def _set_changes(
    kind: Literal["data", "thesis"],
    key: str,
    before: Iterable[str],
    after: Iterable[str],
    label: str,
) -> list[Change]:
    b, a = set(before), set(after)
    out = []
    for item in sorted(a - b):
        out.append(Change(kind=kind, key=key, after=item, message=f"new {label}: {item}"))
    for item in sorted(b - a):
        out.append(Change(kind=kind, key=key, before=item, message=f"{label} cleared: {item}"))
    return out


def diff(prev: Snapshot, cur: Snapshot) -> list[Change]:
    changes: list[Change] = []
    pd, cd = prev.data, cur.data
    for key in sorted(set(pd) | set(cd)):
        if key == "unavailable":
            continue
        b, a = pd.get(key), cd.get(key)
        kind, _, name = key.partition(":")
        if kind == "filing":
            if b is None or a is None:
                if b is None and a is not None:
                    msg = f"{name}: now reported ({a['value']} as of {a['as_of']})"
                else:
                    msg = f"{name}: no longer reported"
            elif a["as_of"] != b["as_of"]:
                msg = f"{name}: new period {b['as_of']} -> {a['as_of']} ({b['value']} -> {a['value']})"
            elif _rel(b["value"], a["value"]) > 1e-9:
                msg = f"{name}: restated for {a['as_of']} ({b['value']} -> {a['value']})"
            else:
                continue
            changes.append(Change(kind="data", key=key, before=b, after=a, message=msg))
        elif kind == "consensus":
            bv, av = (b or {}).get("value"), (a or {}).get("value")
            limit = RECOMMENDATION_ABS if name == "recommendation_mean" else None
            shifted = (
                _reaches(abs(av - bv), limit)
                if limit and isinstance(av, int | float) and isinstance(bv, int | float)
                else _reaches(_rel(bv, av), CONSENSUS_REL)
            )
            if shifted:
                changes.append(
                    Change(
                        kind="data", key=key, before=bv, after=av, message=f"{name}: {bv} -> {av}"
                    )
                )
        elif kind == "forecast":
            b, a = b or {}, a or {}
            if b.get("status") != a.get("status"):
                msg = f"{name}: {b.get('status', 'absent')} -> {a.get('status', 'absent')}"
            else:
                moved = [
                    f"{k} {b.get(k)} -> {a.get(k)}"
                    for k in ("low", "mean", "high")
                    if _reaches(_rel(b.get(k), a.get(k)), CONSENSUS_REL)
                ]
                bc, ac = b.get("analyst_count"), a.get("analyst_count")
                if (
                    isinstance(bc, int)
                    and isinstance(ac, int)
                    and _reaches(abs(ac - bc), ANALYST_COUNT_ABS)
                ):
                    moved.append(f"analysts {bc} -> {ac}")
                if not moved:
                    continue
                msg = f"{name}: " + ", ".join(moved)
            changes.append(Change(kind="data", key=key, before=b, after=a, message=msg))
    changes += _set_changes(
        "data",
        "unavailable",
        pd.get("unavailable", []),
        cd.get("unavailable", []),
        "unavailable source",
    )

    pt, ct = prev.thesis, cur.thesis
    if pt.get("scenario_mode") != ct.get("scenario_mode"):
        changes.append(
            Change(
                kind="thesis",
                key="scenario_mode",
                before=pt.get("scenario_mode"),
                after=ct.get("scenario_mode"),
                message=f"scenario mode: {pt.get('scenario_mode')} -> {ct.get('scenario_mode')}",
            )
        )
    rng, price = pt.get("scenario_range"), ct.get("price")
    if rng and isinstance(price, int | float) and not rng[0] <= price <= rng[1]:
        side = "below the prior Bear low" if price < rng[0] else "above the prior Bull high"
        changes.append(
            Change(
                kind="thesis",
                key="scenario_range",
                before=rng,
                after=price,
                message=f"price {price:.2f} is {side} (prior range {rng[0]:.2f}-{rng[1]:.2f})",
            )
        )
    for key, label in (
        ("red_flags", "red flag"),
        ("quality_gate_failures", "quality gate failure"),
        ("blocking_reviews", "blocking review finding"),
    ):
        changes += _set_changes("thesis", key, pt.get(key, []), ct.get(key, []), label)
    return changes


Runner = Callable[[AnalysisRequest], AnalysisResult]


class ItemCheck(BaseModel):
    ticker: str
    run_id: str | None = None
    status: Literal["baseline", "unchanged", "alert", "failed"]
    alert_id: int | None = None
    changes: list[Change] = Field(default_factory=list)
    error: str | None = None


def check_item(
    store: RunStore, watchlist_id: int, ticker: str, horizon: str, runner: Runner
) -> ItemCheck:
    """Run one ticker, persist the run, compare with the stored baseline and record
    an alert only when something material changed. A failed run never alerts and
    keeps the old baseline, so a flaky provider cannot fake a change."""
    try:
        result = runner(AnalysisRequest(ticker=ticker, horizon=Horizon(horizon)))
    except Exception as exc:
        store.update_item(watchlist_id, ticker, error=f"{type(exc).__name__}: {exc}")
        return ItemCheck(ticker=ticker, status="failed", error=str(exc))
    store.save(result)
    if result.status != RunStatus.COMPLETE:
        store.update_item(watchlist_id, ticker, run_id=result.run_id, error=result.error)
        return ItemCheck(ticker=ticker, run_id=result.run_id, status="failed", error=result.error)

    cur = snapshot(result)
    prev_json = store.item_baseline(watchlist_id, ticker)
    store.update_item(
        watchlist_id, ticker, run_id=result.run_id, baseline=cur.model_dump_json(), error=None
    )
    if prev_json is None:
        return ItemCheck(ticker=ticker, run_id=result.run_id, status="baseline")
    prev = Snapshot.model_validate_json(prev_json)
    changes = diff(prev, cur)
    if not changes:
        return ItemCheck(ticker=ticker, run_id=result.run_id, status="unchanged")
    alert_id = store.add_alert(
        watchlist_id,
        ticker,
        run_id=result.run_id,
        prev_run_id=prev.run_id,
        changes=[c.model_dump(mode="json") for c in changes],
    )
    return ItemCheck(
        ticker=ticker, run_id=result.run_id, status="alert", alert_id=alert_id, changes=changes
    )


def check_watchlist(store: RunStore, watchlist_id: int, runner: Runner) -> list[ItemCheck]:
    wl = store.get_watchlist(watchlist_id)
    if wl is None:
        raise KeyError(watchlist_id)
    return [
        check_item(store, watchlist_id, item["ticker"], wl["horizon"], runner)
        for item in wl["items"]
    ]
