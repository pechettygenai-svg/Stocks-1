"""Today's biggest US gainers, losers and most-active stocks, from Yahoo Finance's predefined
screeners. Market data only: nothing is stored, and a provider failure is reported as
status "unavailable" instead of an empty "no movers" list."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

SOURCE_NOTE = "Yahoo Finance screener via yfinance; may be delayed."
CRITERIA = "US-listed stocks with market cap of at least $2B"
KINDS: dict[str, tuple[str, str]] = {
    "gainers": ("day_gainers", "Up more than 3% today, price at least $5"),
    "losers": ("day_losers", "Down more than 2.5% today, price at least $5"),
    "active": ("most_actives", "More than 5 million shares traded today"),
}
MAX_COUNT = 100
CACHE_SECONDS = 120

Screener = Callable[[str, int], list[dict[str, Any]]]

_cache: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}
_lock = threading.Lock()


def _yahoo_screen(name: str, count: int) -> list[dict[str, Any]]:  # pragma: no cover - network path
    import yfinance as yf  # noqa: PLC0415

    return list((yf.screen(name, count=count) or {}).get("quotes") or [])


def _num(value: Any) -> float | None:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _iso(epoch: Any) -> str | None:
    t = _num(epoch)
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds") if t else None


def _row(q: dict[str, Any]) -> dict[str, Any] | None:
    symbol = q.get("symbol")
    price = _num(q.get("regularMarketPrice"))
    pct = _num(q.get("regularMarketChangePercent"))
    if not isinstance(symbol, str) or not symbol or price is None or price <= 0 or pct is None:
        return None
    name = q.get("displayName") or q.get("shortName") or q.get("longName") or ""
    return {
        "symbol": symbol,
        "name": name if isinstance(name, str) else "",
        "price": price,
        "change": _num(q.get("regularMarketChange")),
        "change_pct": pct,
        "volume": _num(q.get("regularMarketVolume")),
        "avg_volume_3m": _num(q.get("averageDailyVolume3Month")),
        "market_cap": _num(q.get("marketCap")),
        "exchange": q.get("fullExchangeName")
        if isinstance(q.get("fullExchangeName"), str)
        else None,
        "currency": q.get("currency") if isinstance(q.get("currency"), str) else None,
        "market_time": _iso(q.get("regularMarketTime")),
    }


def get_movers(kind: str, count: int = 25, screen: Screener | None = None) -> dict[str, Any]:
    """Movers for `kind` (a key of KINDS). Successful results are cached for CACHE_SECONDS."""
    name, rule = KINDS[kind]
    key = (kind, count)
    with _lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
    out: dict[str, Any] = {
        "kind": kind,
        "criteria": f"{CRITERIA}. {rule}.",
        "source": SOURCE_NOTE,
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "ok",
        "error": None,
        "market_state": None,
        "movers": [],
    }
    try:
        quotes = (screen or _yahoo_screen)(name, count)
    except Exception as exc:  # noqa: BLE001 - any provider failure is reported, not raised
        out.update(status="unavailable", error=str(exc)[:200] or type(exc).__name__)
        return out
    rows = [r for r in (_row(q) for q in quotes if isinstance(q, dict)) if r is not None]
    if kind == "gainers":
        rows.sort(key=lambda r: -r["change_pct"])
    elif kind == "losers":
        rows.sort(key=lambda r: r["change_pct"])
    else:
        rows.sort(key=lambda r: -(r["volume"] or 0))
    states = [
        q.get("marketState")
        for q in quotes
        if isinstance(q, dict) and isinstance(q.get("marketState"), str)
    ]
    out.update(movers=rows[:count], market_state=states[0] if states else None)
    with _lock:
        _cache[key] = (time.monotonic(), out)
    return out
