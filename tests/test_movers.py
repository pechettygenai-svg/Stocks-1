"""GET /v1/movers: Yahoo day gainers/losers/most-active, offline via a fake screener."""

import pytest
from fastapi.testclient import TestClient

import stock_forecaster.api as api
from stock_forecaster import movers

SAMPLE = [
    {
        "symbol": "AAA",
        "shortName": "Alpha",
        "regularMarketPrice": 10.0,
        "regularMarketChangePercent": 5.5,
        "regularMarketChange": 0.52,
        "regularMarketVolume": 1_000_000,
        "marketCap": 3e9,
        "marketState": "REGULAR",
        "regularMarketTime": 1795035600,
        "fullExchangeName": "NasdaqGS",
        "currency": "USD",
    },
    {
        "symbol": "BBB",
        "displayName": "Beta",
        "regularMarketPrice": 50.0,
        "regularMarketChangePercent": 12.1,
        "regularMarketVolume": 9_000_000,
    },
    {"symbol": "NOPX", "regularMarketChangePercent": 9.0},
    {"symbol": "NAN", "regularMarketPrice": 5.0, "regularMarketChangePercent": float("nan")},
    "not a dict",
]


@pytest.fixture(autouse=True)
def _clear_cache():
    movers._cache.clear()
    yield
    movers._cache.clear()


def test_gainers_are_mapped_sorted_and_cleaned(monkeypatch):
    calls = []

    def fake(name, count):
        calls.append((name, count))
        return SAMPLE

    monkeypatch.setattr(movers, "_yahoo_screen", fake)
    r = TestClient(api.app).get("/v1/movers", params={"kind": "gainers", "count": 10})
    assert r.status_code == 200
    body = r.json()
    assert calls == [("day_gainers", 10)]
    assert body["status"] == "ok" and body["market_state"] == "REGULAR"
    assert [m["symbol"] for m in body["movers"]] == ["BBB", "AAA"]
    a = body["movers"][1]
    assert (
        a["name"] == "Alpha"
        and a["price"] == 10.0
        and a["change_pct"] == 5.5
        and a["market_cap"] == 3e9
    )
    assert a["market_time"].startswith("2026-11-") and body["movers"][0]["change"] is None
    assert "Up more than 3%" in body["criteria"] and "Yahoo" in body["source"]


def test_losers_sort_ascending_and_results_are_cached(monkeypatch):
    calls = []
    rows = [
        {"symbol": s, "regularMarketPrice": 9.0, "regularMarketChangePercent": p}
        for s, p in [("X", -3.0), ("Y", -9.0)]
    ]
    monkeypatch.setattr(movers, "_yahoo_screen", lambda n, c: calls.append(n) or rows)
    client = TestClient(api.app)
    first = client.get("/v1/movers", params={"kind": "losers"}).json()
    second = client.get("/v1/movers", params={"kind": "losers"}).json()
    assert [m["symbol"] for m in first["movers"]] == ["Y", "X"]
    assert second == first and calls == ["day_losers"]


def test_provider_failure_is_reported_and_not_cached(monkeypatch):
    def boom(name, count):
        raise RuntimeError("yahoo down")

    monkeypatch.setattr(movers, "_yahoo_screen", boom)
    body = TestClient(api.app).get("/v1/movers", params={"kind": "active"}).json()
    assert (
        body["status"] == "unavailable" and body["movers"] == [] and "yahoo down" in body["error"]
    )
    assert movers._cache == {}


@pytest.mark.parametrize("params", [{"kind": "winners"}, {"count": 0}, {"count": 101}])
def test_bad_parameters_are_rejected(params):
    assert TestClient(api.app).get("/v1/movers", params=params).status_code == 422
