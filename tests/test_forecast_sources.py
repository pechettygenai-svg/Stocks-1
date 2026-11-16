from conftest import load

from stock_forecaster.adapters import (
    AdapterError,
    AlphaVantageAdapter,
    FinnhubAdapter,
    FmpAdapter,
    SecEdgarAdapter,
    YahooAdapter,
)
from stock_forecaster.llm import NullProvider
from stock_forecaster.models import AnalysisRequest, RetrievalStatus
from stock_forecaster.pipeline import default_adapters, run_analysis
from stock_forecaster.watch import diff, snapshot

PAYLOADS = load("forecast_apis_aapl.json")


def _run(aapl_yahoo, aapl_sec, *keyed):
    adapters = [
        YahooAdapter(info=aapl_yahoo["info"], history=aapl_yahoo["history"]),
        SecEdgarAdapter(facts=aapl_sec, cik=aapl_sec["cik"]),
        *keyed,
    ]
    return run_analysis(AnalysisRequest(ticker="AAPL"), adapters=adapters, provider=NullProvider())


def _rows(result, source):
    return {f.forecast_type: f for f in result.forecasts if f.source == source}


def _no_network(*_):
    raise AssertionError("network used")


def test_without_keys_sources_are_listed_as_not_configured(aapl_yahoo, aapl_sec):
    keyed = [cls(fetch=_no_network) for cls in (FinnhubAdapter, FmpAdapter, AlphaVantageAdapter)]
    result = _run(aapl_yahoo, aapl_sec, *keyed)
    for src, var in (
        ("Finnhub", "FINNHUB_API_KEY"),
        ("Financial Modeling Prep", "FMP_API_KEY"),
        ("Alpha Vantage", "ALPHAVANTAGE_API_KEY"),
    ):
        [row] = _rows(result, src).values()
        assert row.status == RetrievalStatus.UNAVAILABLE and var in row.notes[0]
    assert {"finnhub", "fmp", "alphavantage"} <= set(AnalysisRequest(ticker="X").include_sources)
    assert {a.name for a in default_adapters(AnalysisRequest(ticker="X"))} >= {"finnhub", "fmp"}


def test_keyed_feeds_become_panel_rows(aapl_yahoo, aapl_sec):
    keyed = [
        FinnhubAdapter(payloads=PAYLOADS["finnhub"]),
        FmpAdapter(payloads=PAYLOADS["fmp"]),
        AlphaVantageAdapter(payloads=PAYLOADS["alphavantage"]),
    ]
    result = _run(aapl_yahoo, aapl_sec, *keyed)

    fh = _rows(result, "Finnhub")
    target = fh["analyst price target (consensus)"]
    assert (target.value_low, target.value_mean, target.value_high) == (220.0, 312.5, 400.0)
    assert str(target.as_of) == "2026-11-10"
    mix = fh["analyst rating mix"]
    # latest period wins: 9/24/11/1/0
    assert mix.ratings == {"strong_buy": 9, "buy": 24, "hold": 11, "sell": 1, "strong_sell": 0}
    assert mix.analyst_count == 45 and mix.rating_mean == round(94 / 45, 2)
    assert mix.value_mean is None and not mix.outlier

    fmp = _rows(result, "Financial Modeling Prep")["analyst price target (consensus)"]
    assert fmp.value_mean == 305.4
    fmp_ev = next(e for e in result.evidence if e.id == fmp.evidence_ids[0])
    assert "no as-of date" in (fmp_ev.notes or "")

    # Alpha Vantage republishes the same consensus as Yahoo: flagged, counted once.
    av = _rows(result, "Alpha Vantage")["analyst price target (consensus)"]
    assert av.duplicate_of == "Yahoo Finance (analyst consensus)" and av.weight == "duplicate"

    report = result.report_markdown or ""
    assert "rating mean 2.09 (SB/B/H/S/SS 9/24/11/1/0)" in report
    assert not result.quality_gate_failures


def test_partial_failure_keeps_what_came_back():
    def fetch(url, params, headers):
        if url.endswith("price-target"):
            raise AdapterError("HTTP 403: key rejected or endpoint not in plan")
        return PAYLOADS["finnhub"]["recommendation"]

    records = FinnhubAdapter(api_key="k", fetch=fetch).collect("AAPL")
    fields = {r.field for r in records}
    assert "ext_ratings_finnhub_buy" in fields and "ext_target_finnhub_mean" not in fields
    [status] = [r for r in records if r.retrieval_status == RetrievalStatus.UNAVAILABLE]
    assert "price_target: HTTP 403" in status.claim


def test_rate_limit_message_is_reported_not_parsed():
    records = AlphaVantageAdapter(
        payloads={"overview": {"Information": "Thank you for using Alpha Vantage! rate limit"}}
    ).collect("AAPL")
    [r] = records
    assert r.retrieval_status == RetrievalStatus.UNAVAILABLE and "rate limit" in r.claim


def test_api_key_never_reaches_evidence(aapl_yahoo, aapl_sec):
    secret = "sk-test-DO-NOT-LEAK"
    seen = []

    def fetch(url, params, headers):
        seen.append((url, params, headers))
        return PAYLOADS["alphavantage"]["overview"] if "alphavantage" in url else []

    result = _run(
        aapl_yahoo,
        aapl_sec,
        AlphaVantageAdapter(api_key=secret, fetch=fetch),
        FinnhubAdapter(api_key=secret, fetch=fetch),
    )
    assert seen and secret not in result.model_dump_json()
    # Finnhub sends the key as a header, not in the URL/query string
    assert all(secret not in str(p) for u, p, h in seen if "finnhub" in u)


def test_rating_mix_shift_is_a_watchlist_data_change(aapl_yahoo, aapl_sec):
    before = _run(aapl_yahoo, aapl_sec, FinnhubAdapter(payloads=PAYLOADS["finnhub"]))
    shifted = {
        **PAYLOADS["finnhub"],
        "recommendation": [
            {
                "buy": 10,
                "hold": 25,
                "period": "2026-12-01",
                "sell": 8,
                "strongBuy": 2,
                "strongSell": 0,
            }
        ],
    }
    after = _run(aapl_yahoo, aapl_sec, FinnhubAdapter(payloads=shifted))
    msgs = [c.message for c in diff(snapshot(before), snapshot(after))]
    assert any("Finnhub" in m and "rating mean" in m for m in msgs)


def test_target_only_change_alerts_alongside_rating_mix(aapl_yahoo, aapl_sec):
    before = _run(aapl_yahoo, aapl_sec, FinnhubAdapter(payloads=PAYLOADS["finnhub"]))
    moved = {
        **PAYLOADS["finnhub"],
        "price_target": {**PAYLOADS["finnhub"]["price_target"], "targetMean": 340.0},
    }
    after = _run(aapl_yahoo, aapl_sec, FinnhubAdapter(payloads=moved))
    msgs = [c.message for c in diff(snapshot(before), snapshot(after))]
    assert any("Finnhub" in m and "mean 312.5 -> 340.0" in m for m in msgs)
