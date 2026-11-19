"""Latest prices for a list of tickers, e.g. to value a portfolio. Market data only:
no analysis is run and nothing is stored. Each quote carries its own status so one
unknown symbol doesn't fail the whole batch."""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from .models import normalize_ticker

MAX_TICKERS = 50
VALID_TICKER = re.compile(r"^[A-Za-z0-9.\-^=/]{1,20}$")
SOURCE_NOTE = "Yahoo Finance via yfinance; may be delayed."

Fetcher = Callable[[str], dict[str, Any]]


def _yahoo_fetch(symbol: str) -> dict[str, Any]:  # pragma: no cover - network path
    import yfinance as yf  # noqa: PLC0415

    fi = yf.Ticker(symbol).fast_info
    return {"price": fi.last_price, "previous_close": fi.previous_close, "currency": fi.currency}


def _num(value: Any) -> float | None:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and x > 0 else None


def get_quotes(tickers: list[str], fetch: Fetcher | None = None) -> list[dict[str, Any]]:
    fetch = fetch or _yahoo_fetch

    def one(ticker: str) -> dict[str, Any]:
        symbol = normalize_ticker(ticker)
        out: dict[str, Any] = {
            "ticker": ticker,
            "symbol": symbol,
            "status": "ok",
            "price": None,
            "previous_close": None,
            "currency": None,
            "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "error": None,
        }
        try:
            data = fetch(symbol)
        except Exception as exc:  # noqa: BLE001 - any provider failure is reported per ticker
            out.update(status="unavailable", error=str(exc)[:200] or type(exc).__name__)
            return out
        price = _num(data.get("price"))
        if price is None:
            out.update(status="unavailable", error="no price returned (unknown or delisted symbol)")
            return out
        currency = data.get("currency")
        out.update(
            price=price,
            previous_close=_num(data.get("previous_close")),
            currency=currency if isinstance(currency, str) else None,
        )
        return out

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(tickers)))) as pool:
        return list(pool.map(one, tickers))
