"""Data for a per-stock page (overview, financials, filings, news, technicals, forecasts,
seasonals, options, ETFs). Market data comes from Yahoo Finance via yfinance and may be
delayed; financials and filings come from SEC EDGAR. Every section returns
{"status": "ok", ...} or {"status": "unavailable", "error": ...}; nothing is made up to
fill a gap, and analyst numbers are labeled as third-party opinions."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, TypeVar

import httpx

from .adapters.sec import CONCEPTS, IFRS_CONCEPTS, TICKERS_URL, _annual_series, _user_agent
from .analytics.technicals import rsi, sma
from .models import normalize_ticker

YAHOO = "Yahoo Finance via yfinance; may be delayed."
SEC = "SEC EDGAR (company filings)."
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
POPULAR_ETFS = [
    "SPY", "VOO", "IVV", "VTI", "QQQ", "DIA", "IWM", "RSP", "XLK", "XLF", "XLV", "XLE",
    "XLY", "XLP", "XLI", "XLC", "XLU", "XLB", "XLRE", "SMH", "VGT", "ARKK",
]  # fmt: skip
SECTIONS = (
    "overview",
    "financials",
    "filings",
    "news",
    "technicals",
    "forecasts",
    "seasonals",
    "options",
    "etfs",
)
TTL = {
    "overview": 120, "news": 300, "technicals": 300, "options": 120, "forecasts": 1800,
    "seasonals": 3600, "filings": 3600, "financials": 6 * 3600, "etf": 6 * 3600, "cik": 24 * 3600,
}  # fmt: skip

T = TypeVar("T")

_cache: dict[tuple[Any, ...], tuple[float, Any]] = {}
_lock = threading.Lock()


def _yf(symbol: str) -> Any:  # pragma: no cover - network path
    import yfinance as yf  # noqa: PLC0415

    return yf.Ticker(symbol)


def _sec_json(url: str) -> dict[str, Any]:  # pragma: no cover - network path
    resp = httpx.get(url, headers={"User-Agent": _user_agent()}, timeout=20.0)
    resp.raise_for_status()
    return dict(resp.json())


def _cached(key: tuple[Any, ...], ttl: int, make: Callable[[], T]) -> T:
    with _lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]  # type: ignore[no-any-return]
    value = make()
    with _lock:
        _cache[key] = (time.monotonic(), value)
    return value


def _num(value: Any) -> float | None:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso_epoch(value: Any) -> str | None:
    t = _num(value)
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds") if t else None


def _bars(frame: Any) -> list[dict[str, Any]]:
    """Daily/monthly OHLCV rows from a yfinance history DataFrame, skipping bad rows."""
    out: list[dict[str, Any]] = []
    if frame is None or getattr(frame, "empty", True):
        return out
    for idx, row in frame.iterrows():
        close = _num(row.get("Close"))
        if close is None or close <= 0:
            continue
        out.append(
            {
                "time": idx.strftime("%Y-%m-%d"),
                "open": _num(row.get("Open")),
                "high": _num(row.get("High")),
                "low": _num(row.get("Low")),
                "close": close,
                "volume": _num(row.get("Volume")),
            }
        )
    return out


def _history(symbol: str, period: str, interval: str = "1d") -> list[dict[str, Any]]:
    return _cached(
        ("hist", symbol, period, interval),
        TTL["technicals"],
        lambda: _bars(_yf(symbol).history(period=period, interval=interval, auto_adjust=False)),
    )


def _info(symbol: str) -> dict[str, Any]:
    return _cached(("info", symbol), TTL["overview"], lambda: dict(_yf(symbol).info or {}))


# --- sections -------------------------------------------------------------------------


def overview(symbol: str) -> dict[str, Any]:
    info = _info(symbol)
    bars = _history(symbol, "1y")
    price = _num(info.get("currentPrice")) or _num(info.get("regularMarketPrice"))
    if price is None and bars:
        price = bars[-1]["close"]
    if price is None:
        raise LookupError("no quote for this symbol (unknown or delisted?)")
    prev = _num(info.get("regularMarketPreviousClose")) or _num(info.get("previousClose"))
    change = price - prev if prev else None
    return {
        "source": YAHOO,
        "name": _str(info.get("longName")) or _str(info.get("shortName")),
        "exchange": _str(info.get("fullExchangeName")) or _str(info.get("exchange")),
        "currency": _str(info.get("currency")),
        "quote_type": _str(info.get("quoteType")),
        "price": price,
        "previous_close": prev,
        "change": change,
        "change_pct": change / prev * 100 if change is not None and prev else None,
        "market_time": _iso_epoch(info.get("regularMarketTime")),
        "market_state": _str(info.get("marketState")),
        "stats": {
            "market_cap": _num(info.get("marketCap")),
            "pe_trailing": _num(info.get("trailingPE")),
            "pe_forward": _num(info.get("forwardPE")),
            "eps_ttm": _num(info.get("epsTrailingTwelveMonths")) or _num(info.get("trailingEps")),
            # yfinance reports dividendYield in percent units (0.41 = 0.41%)
            "dividend_yield_pct": _num(info.get("dividendYield")),
            "beta": _num(info.get("beta")),
            "year_high": _num(info.get("fiftyTwoWeekHigh")),
            "year_low": _num(info.get("fiftyTwoWeekLow")),
            "day_high": _num(info.get("dayHigh")),
            "day_low": _num(info.get("dayLow")),
            "volume": _num(info.get("volume")) or _num(info.get("regularMarketVolume")),
            "avg_volume": _num(info.get("averageVolume")),
            "shares_outstanding": _num(info.get("sharesOutstanding")),
            "next_earnings": _iso_epoch(info.get("earningsTimestamp")),
        },
        "profile": {
            "sector": _str(info.get("sector")),
            "industry": _str(info.get("industry")),
            "employees": _num(info.get("fullTimeEmployees")),
            "website": _str(info.get("website")),
            "city": _str(info.get("city")),
            "country": _str(info.get("country")),
            "summary": _str(info.get("longBusinessSummary")),
        },
        "history": bars,
    }


def _cik(symbol: str) -> int:
    rows = _cached(("cik",), TTL["cik"], lambda: list(_sec_json(TICKERS_URL).values()))
    want = normalize_ticker(symbol)
    for r in rows:
        if normalize_ticker(str(r.get("ticker", ""))) == want:
            return int(r["cik_str"])
    raise LookupError(
        "not in the SEC ticker list (ETF, fund or foreign listing without SEC filings?)"
    )


FIN_FIELDS = [
    ("revenue", "revenue_fy"), ("operating_income", "operating_income_fy"), ("net_income", "net_income_fy"),
    ("eps_diluted", "eps_diluted_fy"), ("operating_cash_flow", "operating_cash_flow_fy"), ("capex", "capex_fy"),
    ("cash", "cash"), ("long_term_debt", "long_term_debt"), ("total_equity", "total_equity"),
    ("total_assets", "total_assets"),
]  # fmt: skip


def financials(symbol: str) -> dict[str, Any]:
    cik = _cik(symbol)
    facts = _cached(("facts", cik), TTL["financials"], lambda: _sec_json(FACTS_URL.format(cik=cik)))
    taxonomy, cmap = "us-gaap", CONCEPTS
    tax = facts.get("facts", {}).get("us-gaap", {})
    if not _annual_series(tax, CONCEPTS["revenue_fy"][0], "duration") and facts.get(
        "facts", {}
    ).get("ifrs-full"):
        taxonomy, cmap, tax = "ifrs-full", IFRS_CONCEPTS, facts["facts"]["ifrs-full"]
    series: dict[str, list[dict[str, Any]]] = {}
    concepts: dict[str, str] = {}
    for out_key, field in FIN_FIELDS:
        if field not in cmap:
            continue
        names, kind, _ = cmap[field]
        pts = _annual_series(tax, names, kind)
        if pts:
            series[out_key] = pts
            concepts[out_key] = pts[0]["concept"]
    anchor = series.get("revenue") or series.get("net_income")
    if not anchor:
        raise LookupError("no annual figures in this company's XBRL filings")
    years = []
    for pt in anchor[:5]:
        row: dict[str, Any] = {
            "end": pt["end"].isoformat(),
            "fiscal_year": pt["fy"],
            "form": pt["form"],
            "filed": pt["filed_str"],
        }
        for key, pts in series.items():
            match = next((p for p in pts if abs((p["end"] - pt["end"]).days) <= 7), None)
            row[key] = match["val"] if match else None
        ocf, capex = row.get("operating_cash_flow"), row.get("capex")
        row["free_cash_flow"] = ocf - capex if ocf is not None and capex is not None else None
        years.append(row)
    return {
        "source": SEC,
        "cik": cik,
        "company": _str(facts.get("entityName")),
        "taxonomy": taxonomy,
        "currency": anchor[0]["unit"],
        "concepts": concepts,
        "years": list(reversed(years)),
        "url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}",
    }


def filings(symbol: str) -> dict[str, Any]:
    cik = _cik(symbol)
    data = _cached(
        ("subs", cik), TTL["filings"], lambda: _sec_json(SUBMISSIONS_URL.format(cik=cik))
    )
    recent = data.get("filings", {}).get("recent", {})
    cols = [
        "accessionNumber",
        "filingDate",
        "reportDate",
        "form",
        "primaryDocument",
        "primaryDocDescription",
    ]
    n = min(len(recent.get(c, [])) for c in cols) if all(c in recent for c in cols) else 0
    rows = []
    for i in range(min(n, 200)):
        accn = str(recent["accessionNumber"][i])
        folder = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn.replace('-', '')}"
        doc = str(recent["primaryDocument"][i] or "")
        rows.append(
            {
                "form": str(recent["form"][i]),
                "filed": str(recent["filingDate"][i]),
                "period": str(recent["reportDate"][i] or "") or None,
                "description": str(recent["primaryDocDescription"][i] or "") or None,
                "url": f"{folder}/{doc}" if doc else f"{folder}/",
                "index_url": f"{folder}/{accn}-index.htm",
            }
        )
    return {"source": SEC, "cik": cik, "company": _str(data.get("name")), "filings": rows,
            "url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"}  # fmt: skip


def news(symbol: str) -> dict[str, Any]:
    items = _cached(("news", symbol), TTL["news"], lambda: list(_yf(symbol).news or []))
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        c: dict[str, Any] = it["content"] if isinstance(it.get("content"), dict) else it
        url = (
            (c.get("canonicalUrl") or {}).get("url")
            if isinstance(c.get("canonicalUrl"), dict)
            else c.get("link")
        )
        provider: dict[str, Any] = c["provider"] if isinstance(c.get("provider"), dict) else {}
        thumb: dict[str, Any] = c["thumbnail"] if isinstance(c.get("thumbnail"), dict) else {}
        res: list[Any] = thumb["resolutions"] if isinstance(thumb.get("resolutions"), list) else []
        title = _str(c.get("title"))
        if not title or not _str(url) or not str(url).startswith(("https://", "http://")):
            continue
        out.append(
            {
                "title": title,
                "summary": _str(c.get("summary")) or _str(c.get("description")),
                "publisher": _str(provider.get("displayName")) or _str(c.get("publisher")),
                "published": _str(c.get("pubDate")) or _iso_epoch(c.get("providerPublishTime")),
                "url": url,
                "thumbnail": next(
                    (
                        r.get("url")
                        for r in res
                        if isinstance(r, dict) and str(r.get("url", "")).startswith("https://")
                    ),
                    None,
                ),
            }
        )
    return {"source": YAHOO, "articles": out}


def _ema_series(values: list[float], span: int) -> list[float]:
    k = 2 / (span + 1)
    out: list[float] = []
    for v in values:
        out.append(v if not out else v * k + out[-1] * (1 - k))
    return out


def technicals(symbol: str) -> dict[str, Any]:
    bars = _history(symbol, "2y")
    closes = [b["close"] for b in bars]
    if len(closes) < 35:
        raise LookupError("not enough price history for indicators")
    last = closes[-1]
    s20, s50, s200 = sma(closes, 20), sma(closes, 50), sma(closes, 200)
    r = rsi(closes, 14)
    e12, e26 = _ema_series(closes, 12), _ema_series(closes, 26)
    macd = [a - b for a, b in zip(e12, e26, strict=True)][25:]
    sig = _ema_series(macd, 9)
    std20 = math.sqrt(sum((c - s20) ** 2 for c in closes[-20:]) / 19) if s20 is not None else None
    upper = s20 + 2 * std20 if s20 is not None and std20 is not None else None
    lower = s20 - 2 * std20 if s20 is not None and std20 is not None else None
    year = closes[-252:]
    signals: list[dict[str, Any]] = []

    def add(name: str, value: float | None, signal: str, rule: str) -> None:
        signals.append({"indicator": name, "value": value, "signal": signal, "rule": rule})

    for label, ma in (("20-day SMA", s20), ("50-day SMA", s50), ("200-day SMA", s200)):
        if ma is not None:
            add(
                label,
                ma,
                "bullish" if last > ma else "bearish",
                "Price above the average is bullish, below is bearish",
            )
    if s50 is not None and s200 is not None:
        add(
            "50 vs 200-day SMA",
            s50 - s200,
            "bullish" if s50 > s200 else "bearish",
            "50-day above 200-day is bullish",
        )
    if r is not None:
        add(
            "RSI (14)",
            r,
            "bullish" if r < 30 else "bearish" if r > 70 else "neutral",
            "Below 30 oversold (bullish), above 70 overbought (bearish)",
        )
    add(
        "MACD (12, 26, 9)",
        macd[-1],
        "bullish" if macd[-1] > sig[-1] else "bearish",
        "MACD above its signal line is bullish",
    )
    if upper is not None and lower is not None:
        add("Bollinger (20, 2)", (last - lower) / (upper - lower) if upper > lower else None,
            "bullish" if last < lower else "bearish" if last > upper else "neutral",
            "Value is %B: below the lower band (<0) bullish, above the upper band (>1) bearish")  # fmt: skip
    counts = {
        k: sum(1 for s in signals if s["signal"] == k) for k in ("bullish", "neutral", "bearish")
    }
    return {
        "source": YAHOO,
        "as_of": bars[-1]["time"],
        "price": last,
        "signals": signals,
        "counts": counts,
        "values": {
            "sma20": s20, "sma50": s50, "sma200": s200, "rsi14": r, "macd": macd[-1], "macd_signal": sig[-1],
            "bollinger_upper": upper, "bollinger_lower": lower, "year_high": max(year), "year_low": min(year),
        },
        "note": "Indicator signals are mechanical rules on past prices, not a forecast or a recommendation.",
    }  # fmt: skip


def _frame_records(frame: Any) -> list[dict[str, Any]]:
    if frame is None or getattr(frame, "empty", True):
        return []
    rows = []
    for idx, row in frame.iterrows():
        rec = {str(k): (_num(v) if not isinstance(v, str) else v) for k, v in row.items()}
        rec["_index"] = str(idx)
        rows.append(rec)
    return rows


def forecasts(symbol: str) -> dict[str, Any]:
    def load() -> dict[str, Any]:
        t = _yf(symbol)
        out: dict[str, Any] = {}
        for name, attr in (("targets", "analyst_price_targets"), ("recommendations", "recommendations"),
                           ("eps", "earnings_estimate"), ("revenue", "revenue_estimate")):  # fmt: skip
            try:
                out[name] = getattr(t, attr)
            except Exception:  # noqa: BLE001 - a missing table is reported as absent
                out[name] = None
        return out

    raw = _cached(("fc", symbol), TTL["forecasts"], load)
    info = _info(symbol)
    tg = raw["targets"] if isinstance(raw["targets"], dict) else {}
    targets = {k: _num(tg.get(k)) for k in ("low", "mean", "median", "high", "current")}
    labels = {
        "0q": "Current quarter",
        "+1q": "Next quarter",
        "0y": "Current year",
        "+1y": "Next year",
    }

    def estimates(frame: Any) -> list[dict[str, Any]]:
        rows = []
        for r in _frame_records(frame):
            rows.append(
                {
                    "period": labels.get(r["_index"], r["_index"]),
                    "avg": r.get("avg"),
                    "low": r.get("low"),
                    "high": r.get("high"),
                    "year_ago": r.get("yearAgoEps", r.get("yearAgoRevenue")),
                    "analysts": r.get("numberOfAnalysts"),
                    "growth": r.get("growth"),
                }  # fmt: skip
            )
        return rows

    recs = []
    for r in _frame_records(raw["recommendations"]):
        if not isinstance(r.get("period"), str):
            continue
        recs.append(
            {k: r.get(k) for k in ("period", "strongBuy", "buy", "hold", "sell", "strongSell")}
        )
    if not any(targets.values()) and not recs:
        raise LookupError("no analyst coverage on Yahoo Finance for this symbol")
    return {
        "source": YAHOO,
        "label": "Analyst opinions collected by Yahoo Finance; not StockSym's view.",
        "retrieved_at": _now(),
        "targets": targets,
        "analysts": _num(info.get("numberOfAnalystOpinions")),
        "consensus": _str(info.get("recommendationKey")),
        "recommendations": recs,
        "eps_estimates": estimates(raw["eps"]),
        "revenue_estimates": estimates(raw["revenue"]),
    }


def seasonals(symbol: str) -> dict[str, Any]:
    bars = _cached(
        ("monthly", symbol),
        TTL["seasonals"],
        lambda: _bars(_yf(symbol).history(period="max", interval="1mo", auto_adjust=True)),
    )
    if len(bars) < 13:
        raise LookupError("not enough monthly history")
    this_month = datetime.now(timezone.utc).strftime("%Y-%m")
    by_year: dict[int, list[float | None]] = {}
    for prev, cur in zip(bars[:-1], bars[1:], strict=True):
        y, m = int(cur["time"][:4]), int(cur["time"][5:7])
        by_year.setdefault(y, [None] * 12)[m - 1] = (cur["close"] / prev["close"] - 1) * 100
    years = sorted(by_year)[-20:]
    avg, up, count = [], [], []
    for m in range(12):
        vals = [
            by_year[y][m]
            for y in years
            if by_year[y][m] is not None and f"{y}-{m + 1:02d}" != this_month
        ]
        clean = [v for v in vals if v is not None]
        count.append(len(clean))
        avg.append(sum(clean) / len(clean) if clean else None)
        up.append(sum(1 for v in clean if v > 0) / len(clean) * 100 if clean else None)
    return {
        "source": YAHOO,
        "years": [{"year": y, "months": by_year[y]} for y in reversed(years)],
        "average": avg, "positive_pct": up, "count": count,
        "partial_month": this_month,
        "note": "Monthly price change, dividend-adjusted. The current month is partial and left out of the averages.",
    }  # fmt: skip


OPT_COLS = [("contractSymbol", "contract"), ("strike", "strike"), ("lastPrice", "last"), ("bid", "bid"), ("ask", "ask"),
            ("change", "change"), ("percentChange", "change_pct"), ("volume", "volume"), ("openInterest", "open_interest"),
            ("impliedVolatility", "iv")]  # fmt: skip


def options(symbol: str, expiry: str | None = None) -> dict[str, Any]:
    t = _yf(symbol)
    expiries = _cached(("exp", symbol), TTL["options"], lambda: list(t.options or []))
    if not expiries:
        raise LookupError("no listed options for this symbol")
    if expiry not in expiries:
        expiry = expiries[0]
    chain = _cached(("chain", symbol, expiry), TTL["options"], lambda: t.option_chain(expiry))

    def rows(frame: Any) -> list[dict[str, Any]]:
        out = []
        for r in _frame_records(frame):
            rec = {
                dst: (r.get(src) if src != "contractSymbol" else str(r.get(src) or ""))
                for src, dst in OPT_COLS
            }
            rec["in_the_money"] = bool(r.get("inTheMoney"))
            out.append(rec)
        return out

    return {
        "source": YAHOO,
        "expiry": expiry,
        "expiries": expiries,
        "underlying": _num(_info(symbol).get("regularMarketPrice"))
        or _num(_info(symbol).get("currentPrice")),
        "calls": rows(chain.calls),
        "puts": rows(chain.puts),
        "note": "Delayed quotes. Implied volatility is annualized (0.25 = 25%).",
    }


def _etf_holdings(etf: str) -> tuple[str | None, dict[str, float]]:
    def load() -> tuple[str | None, dict[str, float]]:
        t = _yf(etf)
        top = t.funds_data.top_holdings
        weights: dict[str, float] = {}
        if top is not None and not getattr(top, "empty", True):
            for sym, row in top.iterrows():
                w = _num(row.get("Holding Percent"))
                if w is not None:
                    weights[normalize_ticker(str(sym))] = w * 100
        try:
            name = _str((t.info or {}).get("shortName"))
        except Exception:  # noqa: BLE001
            name = None
        return name, weights

    return _cached(("etf", etf), TTL["etf"], load)


def etfs(symbol: str, extra: list[str] | None = None) -> dict[str, Any]:
    want = normalize_ticker(symbol)
    candidates = list(dict.fromkeys(POPULAR_ETFS + [normalize_ticker(e) for e in (extra or [])]))

    def one(etf: str) -> tuple[str, str | None, dict[str, float] | None, str | None]:
        try:
            name, weights = _etf_holdings(etf)
            return etf, name, weights, None
        except Exception as exc:  # noqa: BLE001
            return etf, None, None, str(exc)[:120] or type(exc).__name__

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(one, candidates))
    holders: list[dict[str, Any]] = []
    unavailable: list[str] = []
    for etf, name, weights, _err in results:
        if weights is None or not weights:
            unavailable.append(etf)
        elif want in weights:
            holders.append(
                {
                    "etf": etf,
                    "name": name,
                    "weight_pct": weights[want],
                    "rank": sorted(weights.values(), reverse=True).index(weights[want]) + 1,
                }
            )
    holders.sort(key=lambda h: -h["weight_pct"])
    return {
        "source": YAHOO,
        "holders": holders,
        "checked": [r[0] for r in results if r[0] not in unavailable],
        "unavailable": unavailable,
        "note": "Yahoo lists only each ETF's top 10 holdings, so ETFs holding this stock in a smaller weight are not shown.",
    }


def get_section(section: str, symbol: str, **kw: Any) -> dict[str, Any]:
    fn = {
        "overview": overview, "financials": financials, "filings": filings, "news": news,
        "technicals": technicals, "forecasts": forecasts, "seasonals": seasonals, "options": options, "etfs": etfs,
    }[section]  # fmt: skip
    try:
        data = fn(symbol, **kw)
    except Exception as exc:  # noqa: BLE001 - reported per section, never fabricated
        return {"status": "unavailable", "section": section, "symbol": symbol, "retrieved_at": _now(),
                "error": str(exc)[:240] or type(exc).__name__}  # fmt: skip
    return {"status": "ok", "section": section, "symbol": symbol, "retrieved_at": _now(), **data}
