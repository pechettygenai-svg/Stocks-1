"""Yahoo Finance market-data adapter (via the yfinance client).

Yahoo is treated as a tier-3 market-data source: fine for quotes, history and
consensus targets, but reported financials are preferred from SEC filings.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ..models import EvidenceRecord, RetrievalStatus, SourceType
from .base import AdapterError, MarketSnapshot, unavailable

QUOTE_URL = "https://finance.yahoo.com/quote/{ticker}"
ANALYSIS_URL = "https://finance.yahoo.com/quote/{ticker}/analysis"

# info key -> (canonical field, claim label, unit, definition)
_INFO_FIELDS: dict[str, tuple[str, str, str, str]] = {
    "regularMarketPrice": ("price_intraday", "Last price", "currency", "Regular-market last trade"),
    "previousClose": ("price_close", "Previous close", "currency", "Prior session close"),
    "marketCap": ("market_cap", "Market capitalization", "currency", "Price x shares outstanding"),
    "sharesOutstanding": ("shares_outstanding", "Shares outstanding", "shares", "Basic shares"),
    "fiftyTwoWeekLow": ("week52_low", "52-week low", "currency", "Lowest close, trailing 52w"),
    "fiftyTwoWeekHigh": ("week52_high", "52-week high", "currency", "Highest close, trailing 52w"),
    "trailingPE": ("pe_ttm", "Trailing P/E", "multiple", "Price / trailing 12m EPS"),
    "forwardPE": ("pe_forward", "Forward P/E", "multiple", "Price / consensus next-12m EPS"),
    "trailingEps": ("eps_ttm", "Trailing EPS", "currency", "Diluted EPS, trailing 12m"),
    "forwardEps": ("eps_forward", "Forward EPS", "currency", "Consensus next-fiscal-year EPS"),
    "priceToBook": ("price_to_book", "Price to book", "multiple", "Price / book value per share"),
    "trailingAnnualDividendYield": (
        "dividend_yield",
        "Dividend yield",
        "ratio",
        "Trailing annual dividend / price",
    ),
    "shortPercentOfFloat": (
        "short_pct_float",
        "Short interest % of float",
        "ratio",
        "Shares short / float",
    ),
    "beta": ("beta", "Beta", "ratio", "5y monthly beta vs. index"),
    "totalRevenue": ("revenue_ttm", "Revenue (TTM)", "currency", "Yahoo-reported trailing revenue"),
    "netIncomeToCommon": (
        "net_income_ttm",
        "Net income (TTM)",
        "currency",
        "Yahoo-reported net income",
    ),
    "freeCashflow": (
        "free_cash_flow_ttm",
        "Free cash flow (TTM)",
        "currency",
        "Yahoo-reported FCF",
    ),
    "totalCash": (
        "cash_and_short_term_investments",
        "Total cash",
        "currency",
        "Yahoo-reported cash & equivalents",
    ),
    "totalDebt": ("total_debt", "Total debt", "currency", "Yahoo-reported total debt"),
    "returnOnEquity": ("roe", "Return on equity", "ratio", "Net income / avg. equity"),
    "targetLowPrice": (
        "analyst_target_low",
        "Analyst target (low)",
        "currency",
        "Lowest 12m target",
    ),
    "targetMeanPrice": (
        "analyst_target_mean",
        "Analyst target (mean)",
        "currency",
        "Mean 12m target",
    ),
    "targetHighPrice": (
        "analyst_target_high",
        "Analyst target (high)",
        "currency",
        "Highest 12m target",
    ),
    "numberOfAnalystOpinions": ("analyst_count", "Analyst count", "count", "Analysts in consensus"),
    "recommendationMean": (
        "recommendation_mean",
        "Recommendation mean",
        "score",
        "1=strong buy..5=sell",
    ),
}

_TEXT_FIELDS: dict[str, str] = {
    "shortName": "company_name",
    "longName": "company_name",
    "sector": "sector",
    "industry": "industry",
    "exchange": "exchange",
    "currency": "currency",
    "longBusinessSummary": "business_summary",
}


class YahooAdapter:
    name = "yahoo"

    def __init__(self, info: dict[str, Any] | None = None, history: dict[str, Any] | None = None):
        # Optional preloaded payloads (fixtures) so tests never touch the network.
        self._info = info
        self._history = history
        self.snapshot: MarketSnapshot | None = None

    # ------------------------------------------------------------------ fetch
    def _fetch(self, ticker: str) -> tuple[dict[str, Any], dict[str, Any]]:
        if self._info is not None and self._history is not None:
            return self._info, self._history
        try:
            import yfinance as yf  # noqa: PLC0415

            t = yf.Ticker(ticker)
            info = dict(t.info or {})
            hist = t.history(period="1y", interval="1d", auto_adjust=True)
        except Exception as exc:  # pragma: no cover - network path
            raise AdapterError(f"yfinance error: {exc}") from exc
        if not info or info.get("regularMarketPrice") is None:
            raise AdapterError("no quote returned (unknown, delisted or ambiguous symbol)")
        history = {
            "dates": [d.strftime("%Y-%m-%d") for d in hist.index],
            "close": [float(x) for x in hist["Close"].tolist()],
            "volume": [float(x) for x in hist["Volume"].tolist()],
        }
        return info, history

    # ---------------------------------------------------------------- collect
    def collect(self, ticker: str) -> list[EvidenceRecord]:
        url = QUOTE_URL.format(ticker=ticker)
        try:
            info, history = self._fetch(ticker)
        except AdapterError as exc:
            return [unavailable("Yahoo Finance", "market_data", url, str(exc))]

        as_of = _as_of(info, history)
        currency = info.get("currency")
        records: list[EvidenceRecord] = []

        for key, field in _TEXT_FIELDS.items():
            val = info.get(key)
            if isinstance(val, str) and val:
                records.append(
                    EvidenceRecord(
                        claim=f"{field.replace('_', ' ').title()}: {val[:80]}"
                        + ("…" if len(val) > 80 else ""),
                        field=field,
                        value=val,
                        as_of=as_of,
                        source_name="Yahoo Finance",
                        source_url=url,
                        source_type=SourceType.MARKET_DATA,
                        confidence=0.9,
                    )
                )

        for key, (field, label, unit, definition) in _INFO_FIELDS.items():
            val = info.get(key)
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                continue
            is_target = field.startswith("analyst_") or field == "recommendation_mean"
            records.append(
                EvidenceRecord(
                    claim=f"{label}: {val:,.4g}" if abs(val) < 1e6 else f"{label}: {val:,.0f}",
                    field=field,
                    value=float(val),
                    unit=currency if unit == "currency" else unit,
                    currency=currency if unit == "currency" else None,
                    as_of=as_of,
                    period="TTM" if field.endswith("_ttm") else None,
                    definition=definition,
                    source_name="Yahoo Finance" + (" (analyst consensus)" if is_target else ""),
                    source_url=ANALYSIS_URL.format(ticker=ticker) if is_target else url,
                    source_type=SourceType.FORECAST if is_target else SourceType.MARKET_DATA,
                    confidence=0.6 if is_target else 0.8,
                    notes=(
                        "Consensus figure aggregated by Yahoo; methodology not disclosed."
                        if is_target
                        else None
                    ),
                )
            )

        closes = history.get("close", [])
        if closes:
            records.append(
                EvidenceRecord(
                    claim=f"Daily close history: {len(closes)} sessions ending {history['dates'][-1]}",
                    field="price_history",
                    value=float(closes[-1]),
                    unit=currency,
                    currency=currency,
                    as_of=as_of,
                    definition="Split/dividend-adjusted daily closes",
                    source_name="Yahoo Finance",
                    source_url=url + "/history",
                    source_type=SourceType.MARKET_DATA,
                    confidence=0.85,
                    notes=f"first={history['dates'][0]}",
                )
            )
        self.snapshot = MarketSnapshot(
            closes=[float(c) for c in closes],
            volumes=[float(v) for v in history.get("volume", [])],
            dates=list(history.get("dates", [])),
            currency=currency,
            company_name=info.get("longName") or info.get("shortName"),
            exchange=info.get("exchange"),
        )
        for r in records:
            r.retrieval_status = RetrievalStatus.VERIFIED
        return records


def _as_of(info: dict[str, Any], history: dict[str, Any]) -> date:
    ts = info.get("regularMarketTime")
    if isinstance(ts, (int, float)):
        return date.fromtimestamp(ts)
    if history.get("dates"):
        return date.fromisoformat(history["dates"][-1])
    return date.today()
