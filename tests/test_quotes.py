"""GET /v1/quotes: batch prices for portfolio valuation, offline via a fake fetcher."""

from fastapi.testclient import TestClient

import stock_forecaster.api as api
from stock_forecaster import quotes

PRICES = {"AAPL": 231.4, "BRK-B": 593.0}


def _fake(symbol):
    if symbol == "BOOM":
        raise RuntimeError("provider down")
    if symbol == "GONE":
        raise KeyError("currentTradingPeriod")
    return {"price": PRICES.get(symbol), "previous_close": 1.0, "currency": "USD"}


def test_quotes_batch_reports_each_ticker(monkeypatch):
    monkeypatch.setattr(quotes, "_yahoo_fetch", _fake)
    r = TestClient(api.app).get("/v1/quotes", params={"tickers": "aapl, BRK.B,ZZZZ,BOOM,GONE,AAPL"})
    assert r.status_code == 200
    by = {q["ticker"]: q for q in r.json()["quotes"]}
    assert list(by) == ["AAPL", "BRK.B", "ZZZZ", "BOOM", "GONE"]
    assert by["AAPL"]["price"] == 231.4 and by["AAPL"]["status"] == "ok"
    assert by["BRK.B"]["symbol"] == "BRK-B" and by["BRK.B"]["price"] == 593.0
    assert by["ZZZZ"]["status"] == "unavailable" and by["ZZZZ"]["price"] is None
    assert by["BOOM"]["status"] == "unavailable" and "provider down" in by["BOOM"]["error"]
    assert by["GONE"]["error"] == quotes.NO_QUOTE
    assert "Yahoo" in r.json()["source"]


def test_quotes_reject_bad_input(monkeypatch):
    monkeypatch.setattr(quotes, "_yahoo_fetch", _fake)
    c = TestClient(api.app)
    assert c.get("/v1/quotes", params={"tickers": " , "}).status_code == 422
    assert c.get("/v1/quotes", params={"tickers": "AAPL;DROP"}).status_code == 422
    many = ",".join(f"T{i}" for i in range(quotes.MAX_TICKERS + 1))
    assert c.get("/v1/quotes", params={"tickers": many}).status_code == 422


def test_non_positive_or_nan_price_is_unavailable():
    out = quotes.get_quotes(["A", "B"], fetch=lambda s: {"price": float("nan") if s == "A" else -1})
    assert [q["status"] for q in out] == ["unavailable", "unavailable"]
