"""GET /v1/symbol/{ticker}/{section}: the stock page's tabs, offline with fake Yahoo/SEC data."""

import math

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import stock_forecaster.api as api
from stock_forecaster import symbol_data as sd


def _daily(n=260, start=100.0, step=0.5):
    idx = pd.date_range("2025-01-01", periods=n, freq="B", tz="America/New_York")
    close = [start + step * i for i in range(n)]
    return pd.DataFrame({"Open": close, "High": [c + 1 for c in close], "Low": [c - 1 for c in close],
                         "Close": close, "Volume": [1000.0] * n}, index=idx)  # fmt: skip


def _monthly():
    idx = pd.date_range("2023-01-01", periods=30, freq="MS", tz="America/New_York")
    close = [100.0 * (1.02 if i % 2 else 0.99) ** i for i in range(30)]
    return pd.DataFrame(
        {"Open": close, "High": close, "Low": close, "Close": close, "Volume": 1.0}, index=idx
    )


class FakeChain:
    calls = pd.DataFrame([{"contractSymbol": "AAPL261120C00100000", "strike": 100.0, "lastPrice": 5.0, "bid": 4.9,
                           "ask": 5.1, "change": 0.1, "percentChange": 2.0, "volume": float("nan"),
                           "openInterest": 10.0, "impliedVolatility": 0.3, "inTheMoney": True}])  # fmt: skip
    puts = pd.DataFrame(columns=list(calls.columns))


class FakeFunds:
    def __init__(self, sym):
        holdings = {"QQQ": {"AAPL": 0.08, "MSFT": 0.09}, "SPY": {"MSFT": 0.07}}.get(sym)
        if sym == "XLK":
            raise RuntimeError("no fund data")
        self.top_holdings = pd.DataFrame({"Holding Percent": holdings or {}})


class FakeTicker:
    def __init__(self, sym):
        self.sym = sym
        self.info = {} if sym == "NOPE" else {
            "longName": "Apple Inc.", "currentPrice": 230.0, "regularMarketPreviousClose": 200.0,
            "marketCap": 3e12, "trailingPE": 30.0, "dividendYield": 0.41, "sector": "Technology",
            "regularMarketPrice": 230.0, "numberOfAnalystOpinions": 40, "recommendationKey": "buy",
            "shortName": sym, "beta": float("nan"),
        }  # fmt: skip
        self.news = [
            {"id": "1", "content": {"title": "Apple news", "pubDate": "2026-11-18T14:40:24Z",
                                    "canonicalUrl": {"url": "https://finance.yahoo.com/a"}, "provider": {"displayName": "Reuters"}}},
            {"id": "2", "content": {"title": "Bad link", "canonicalUrl": {"url": "javascript:alert(1)"}}},
        ]  # fmt: skip
        self.options = ("2026-11-20", "2026-11-27")
        self.analyst_price_targets = {"low": 150.0, "mean": 240.0, "high": 300.0, "current": 230.0}
        self.recommendations = pd.DataFrame(
            [{"period": "0m", "strongBuy": 5, "buy": 10, "hold": 3, "sell": 1, "strongSell": 0}]
        )
        self.earnings_estimate = pd.DataFrame(
            {"avg": [2.0], "low": [1.8], "high": [2.2], "numberOfAnalysts": [20]}, index=["0q"]
        )
        self.revenue_estimate = None

    @property
    def funds_data(self):
        return FakeFunds(self.sym)

    def history(self, period, interval="1d", auto_adjust=True):
        if self.sym == "NOPE":
            return pd.DataFrame()
        return _monthly() if interval == "1mo" else _daily()

    def option_chain(self, expiry):
        return FakeChain()


FACTS = {"entityName": "Apple Inc.", "facts": {"us-gaap": {
    "Revenues": {"units": {"USD": [
        {"start": "2024-09-29", "end": "2025-09-27", "val": 400.0, "form": "10-K", "fp": "FY", "filed": "2025-11-01"},
        {"start": "2023-10-01", "end": "2024-09-28", "val": 380.0, "form": "10-K", "fp": "FY", "filed": "2024-11-01"}]}},
    "NetIncomeLoss": {"units": {"USD": [
        {"start": "2024-09-29", "end": "2025-09-27", "val": 100.0, "form": "10-K", "fp": "FY", "filed": "2025-11-01"}]}},
    "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": [
        {"start": "2024-09-29", "end": "2025-09-27", "val": 120.0, "form": "10-K", "fp": "FY", "filed": "2025-11-01"}]}},
    "PaymentsToAcquirePropertyPlantAndEquipment": {"units": {"USD": [
        {"start": "2024-09-29", "end": "2025-09-27", "val": 20.0, "form": "10-K", "fp": "FY", "filed": "2025-11-01"}]}},
}}}  # fmt: skip
SUBS = {"name": "Apple Inc.", "filings": {"recent": {
    "accessionNumber": ["0000320193-25-000079"], "filingDate": ["2025-11-01"], "reportDate": ["2025-09-27"],
    "form": ["10-K"], "primaryDocument": ["aapl-20250927.htm"], "primaryDocDescription": ["10-K"]}}}  # fmt: skip


def _sec(url):
    if url == sd.TICKERS_URL:
        return {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}
    if "companyfacts" in url:
        return FACTS
    if "submissions" in url:
        return SUBS
    raise AssertionError(url)


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    sd._cache.clear()
    monkeypatch.setattr(sd, "_yf", FakeTicker)
    monkeypatch.setattr(sd, "_sec_json", _sec)
    yield
    sd._cache.clear()


def get(path, **params):
    r = TestClient(api.app).get(path, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_overview_has_quote_stats_and_history():
    d = get("/v1/symbol/aapl/overview")
    assert d["status"] == "ok" and d["symbol"] == "AAPL" and d["name"] == "Apple Inc."
    assert d["price"] == 230.0 and d["change"] == 30.0 and math.isclose(d["change_pct"], 15.0)
    assert d["stats"]["dividend_yield_pct"] == 0.41 and d["stats"]["beta"] is None
    assert len(d["history"]) == 260 and d["history"][0]["time"] == "2025-01-01"


def test_unknown_symbol_is_unavailable_not_invented():
    d = get("/v1/symbol/NOPE/overview")
    assert d["status"] == "unavailable" and "no quote" in d["error"] and "price" not in d


def test_financials_from_sec_by_period_end():
    d = get("/v1/symbol/AAPL/financials")
    assert d["status"] == "ok" and d["cik"] == 320193 and d["currency"] == "USD"
    last = d["years"][-1]
    assert last["end"] == "2025-09-27" and last["revenue"] == 400.0 and last["net_income"] == 100.0
    assert last["free_cash_flow"] == 100.0
    assert d["years"][0]["net_income"] is None  # not reported for that year -> left empty


def test_filings_and_unknown_sec_symbol():
    d = get("/v1/symbol/AAPL/filings")
    f = d["filings"][0]
    assert f["form"] == "10-K" and f["url"].endswith("/320193/000032019325000079/aapl-20250927.htm")
    assert get("/v1/symbol/QQQ/filings")["status"] == "unavailable"


def test_news_drops_non_http_links():
    d = get("/v1/symbol/AAPL/news")
    assert [a["title"] for a in d["articles"]] == ["Apple news"] and d["articles"][0][
        "publisher"
    ] == "Reuters"


def test_technicals_signals_on_a_rising_series():
    d = get("/v1/symbol/AAPL/technicals")
    sig = {s["indicator"]: s["signal"] for s in d["signals"]}
    assert sig["20-day SMA"] == "bullish" and sig["50 vs 200-day SMA"] == "bullish"
    assert sig["RSI (14)"] == "bearish"  # straight line up: RSI 100, overbought
    assert sum(d["counts"].values()) == len(d["signals"])


def test_forecasts_are_labeled_opinions():
    d = get("/v1/symbol/AAPL/forecasts")
    assert d["targets"]["mean"] == 240.0 and d["consensus"] == "buy" and "opinions" in d["label"]
    assert (
        d["recommendations"][0]["buy"] == 10
        and d["eps_estimates"][0]["period"] == "Current quarter"
    )
    assert d["revenue_estimates"] == []


def test_seasonals_monthly_grid():
    d = get("/v1/symbol/AAPL/seasonals")
    assert len(d["average"]) == 12 and d["years"][0]["year"] == 2025
    assert all(c >= 2 for c in d["count"])


def test_options_chain_and_nan_cleanup():
    d = get("/v1/symbol/AAPL/options", expiry="2026-11-27")
    assert d["expiry"] == "2026-11-27" and d["expiries"] == ["2026-11-20", "2026-11-27"]
    c = d["calls"][0]
    assert (
        c["strike"] == 100.0
        and c["volume"] is None
        and c["in_the_money"] is True
        and d["puts"] == []
    )
    assert get("/v1/symbol/AAPL/options", expiry="2030-01-01")["expiry"] == "2026-11-20"


def test_etfs_holding_the_stock(monkeypatch):
    monkeypatch.setattr(sd, "POPULAR_ETFS", ["QQQ", "SPY", "XLK"])
    d = get("/v1/symbol/AAPL/etfs", etfs="QQQ,schd")
    assert [h["etf"] for h in d["holders"]] == ["QQQ"]
    assert math.isclose(d["holders"][0]["weight_pct"], 8.0) and d["holders"][0]["rank"] == 2
    assert "XLK" in d["unavailable"] and "SCHD" in d["unavailable"] and "SPY" in d["checked"]


def test_results_are_cached(monkeypatch):
    calls = []

    class Counting(FakeTicker):
        def __init__(self, sym):
            calls.append(sym)
            super().__init__(sym)

    monkeypatch.setattr(sd, "_yf", Counting)
    get("/v1/symbol/AAPL/news")
    get("/v1/symbol/AAPL/news")
    assert calls == ["AAPL"]


@pytest.mark.parametrize(
    "path,params",
    [("/v1/symbol/AAPL;DROP/overview", {}), ("/v1/symbol/AAPL/secrets", {}),
     ("/v1/symbol/AAPL/options", {"expiry": "soon"}), ("/v1/symbol/AAPL/etfs", {"etfs": "QQQ,<x>"})],
)  # fmt: skip
def test_bad_input_is_rejected(path, params):
    assert TestClient(api.app).get(path, params=params).status_code == 422
