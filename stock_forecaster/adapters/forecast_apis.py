"""Keyed analyst-forecast feeds with documented public APIs.

Each provider is used only through its official API with the user's own key
(read from the environment, never logged or written to evidence). Without a key
the adapter returns a single `unavailable` record naming the variable to set, so
the comparison panel shows the source as not configured instead of guessing.

Evidence fields (slug has no underscores):
    ext_target_<slug>_<low|mean|median|high>   12m price target
    ext_ratings_<slug>_<strong_buy|buy|hold|sell|strong_sell>   analyst counts
    ext_status_<slug>                          unavailable / not configured
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import date
from typing import Any

import httpx

from ..models import EvidenceRecord, RetrievalStatus, SourceType
from .base import AdapterError

RATING_KINDS = ("strong_buy", "buy", "hold", "sell", "strong_sell")

Fetcher = Callable[[str, dict[str, str], dict[str, str]], Any]


def _http_get(url: str, params: dict[str, str], headers: dict[str, str]) -> Any:  # pragma: no cover
    try:
        resp = httpx.get(url, params=params, headers=headers, timeout=10.0)
    except httpx.HTTPError as exc:
        # The exception text can contain the request URL (and so a query-string key).
        raise AdapterError(f"request failed ({type(exc).__name__})") from None
    if resp.status_code in (401, 402, 403):
        raise AdapterError(f"HTTP {resp.status_code}: key rejected or endpoint not in plan")
    if resp.status_code == 429:
        raise AdapterError("HTTP 429: rate limit reached")
    if resp.status_code >= 400:
        raise AdapterError(f"HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError:
        raise AdapterError("response was not JSON") from None


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # drop NaN


def _date(v: Any) -> date | None:
    if isinstance(v, str) and len(v) >= 10:
        try:
            return date.fromisoformat(v[:10])
        except ValueError:
            return None
    return None


class KeyedForecastAdapter:
    """Shared plumbing: key lookup, fetch with injectable payloads, record shapes."""

    name = ""
    slug = ""
    source_name = ""
    env_var = ""
    page_url = ""
    method_note = "Aggregated by the provider; methodology and contributing analysts not disclosed."

    def __init__(
        self,
        payloads: dict[str, Any] | None = None,
        api_key: str | None = None,
        fetch: Fetcher | None = None,
    ) -> None:
        # `payloads` maps endpoint name -> decoded JSON (fixtures); tests never hit the network.
        self._payloads = payloads
        self._key = api_key if api_key is not None else os.environ.get(self.env_var, "").strip()
        self._fetch = fetch or _http_get

    # ----------------------------------------------------------- subclasses
    def endpoints(self, ticker: str) -> dict[str, tuple[str, dict[str, str], dict[str, str]]]:
        raise NotImplementedError

    def parse(self, ticker: str, data: dict[str, Any]) -> list[EvidenceRecord]:
        raise NotImplementedError

    # --------------------------------------------------------------- shared
    def url(self, ticker: str) -> str:
        return self.page_url.format(ticker=ticker)

    def collect(self, ticker: str) -> list[EvidenceRecord]:
        if self._payloads is None and not self._key:
            return [self._status(ticker, f"not configured; set {self.env_var} to enable")]
        data: dict[str, Any] = {}
        errors: list[str] = []
        for endpoint, (url, params, headers) in self.endpoints(ticker).items():
            try:
                data[endpoint] = (
                    self._payloads.get(endpoint)
                    if self._payloads is not None
                    else self._fetch(url, params, headers)
                )
            except AdapterError as exc:
                errors.append(f"{endpoint}: {exc}")
        records = self.parse(ticker, data)
        if not records:
            reason = "; ".join(errors) or "no forecast data returned for this symbol"
            return [self._status(ticker, reason)]
        for e in errors:
            records.append(self._status(ticker, e))
        return records

    def _status(self, ticker: str, reason: str) -> EvidenceRecord:
        return EvidenceRecord(
            claim=f"{self.source_name} unavailable: {reason}",
            field=f"ext_status_{self.slug}",
            source_name=self.source_name,
            source_url=self.url(ticker),
            source_type=SourceType.FORECAST,
            retrieval_status=RetrievalStatus.UNAVAILABLE,
            confidence=0.0,
            notes=reason,
        )

    def _record(
        self,
        ticker: str,
        field: str,
        value: float,
        label: str,
        as_of: date | None,
        unit: str,
        definition: str,
    ) -> EvidenceRecord:
        dated = as_of is not None
        return EvidenceRecord(
            claim=f"{self.source_name} {label}: {value:,.2f}"
            if unit != "count"
            else f"{self.source_name} {label}: {int(value)}",
            field=field,
            value=value,
            unit=unit,
            as_of=as_of or date.today(),
            definition=definition,
            source_name=self.source_name,
            source_url=self.url(ticker),
            source_type=SourceType.FORECAST,
            retrieval_status=RetrievalStatus.VERIFIED,
            confidence=0.5,
            notes=self.method_note
            + ("" if dated else " Provider gives no as-of date; retrieval date used."),
        )

    def targets(
        self, ticker: str, values: dict[str, Any], as_of: date | None
    ) -> list[EvidenceRecord]:
        out = []
        for kind in ("low", "mean", "median", "high"):
            v = _num(values.get(kind))
            if v is not None and v > 0:
                out.append(
                    self._record(
                        ticker,
                        f"ext_target_{self.slug}_{kind}",
                        v,
                        f"12m price target ({kind})",
                        as_of,
                        "price",
                        f"{kind} of analyst 12-month price targets, listing currency",
                    )
                )
        return out

    def ratings(
        self, ticker: str, counts: dict[str, Any], as_of: date | None
    ) -> list[EvidenceRecord]:
        parsed = {k: _num(counts.get(k)) for k in RATING_KINDS}
        if all(v is None for v in parsed.values()) or not any(parsed.values()):
            return []
        return [
            self._record(
                ticker,
                f"ext_ratings_{self.slug}_{k}",
                v or 0.0,
                f"{k.replace('_', '-')} ratings",
                as_of,
                "count",
                f"Number of analysts rating the stock {k.replace('_', '-')}",
            )
            for k, v in parsed.items()
        ]


class FinnhubAdapter(KeyedForecastAdapter):
    """https://finnhub.io/docs/api/recommendation-trends and /price-target.
    Price targets need a paid plan; on a free key that endpoint reports 403 and
    only the rating mix is used."""

    name = "finnhub"
    slug = "finnhub"
    source_name = "Finnhub"
    env_var = "FINNHUB_API_KEY"
    page_url = "https://finnhub.io/docs/api/recommendation-trends"
    BASE = "https://finnhub.io/api/v1"

    def endpoints(self, ticker: str) -> dict[str, tuple[str, dict[str, str], dict[str, str]]]:
        headers = {"X-Finnhub-Token": self._key}
        params = {"symbol": ticker}
        return {
            "recommendation": (f"{self.BASE}/stock/recommendation", params, headers),
            "price_target": (f"{self.BASE}/stock/price-target", params, headers),
        }

    def parse(self, ticker: str, data: dict[str, Any]) -> list[EvidenceRecord]:
        out: list[EvidenceRecord] = []
        rec = data.get("recommendation")
        if isinstance(rec, list) and rec:
            latest = max(rec, key=lambda r: str(r.get("period", "")))
            out += self.ratings(
                ticker,
                {
                    "strong_buy": latest.get("strongBuy"),
                    "buy": latest.get("buy"),
                    "hold": latest.get("hold"),
                    "sell": latest.get("sell"),
                    "strong_sell": latest.get("strongSell"),
                },
                _date(latest.get("period")),
            )
        pt = data.get("price_target")
        if isinstance(pt, dict):
            out += self.targets(
                ticker,
                {
                    "low": pt.get("targetLow"),
                    "mean": pt.get("targetMean"),
                    "median": pt.get("targetMedian"),
                    "high": pt.get("targetHigh"),
                },
                _date(pt.get("lastUpdated")),
            )
        return out


class FmpAdapter(KeyedForecastAdapter):
    """https://site.financialmodelingprep.com/developer/docs (stable API):
    price-target-consensus and grades-consensus."""

    name = "fmp"
    slug = "fmp"
    source_name = "Financial Modeling Prep"
    env_var = "FMP_API_KEY"
    page_url = "https://site.financialmodelingprep.com/financial-summary/{ticker}"
    BASE = "https://financialmodelingprep.com/stable"

    def endpoints(self, ticker: str) -> dict[str, tuple[str, dict[str, str], dict[str, str]]]:
        headers = {"apikey": self._key}
        params = {"symbol": ticker}
        return {
            "price_target": (f"{self.BASE}/price-target-consensus", params, headers),
            "grades": (f"{self.BASE}/grades-consensus", params, headers),
        }

    def parse(self, ticker: str, data: dict[str, Any]) -> list[EvidenceRecord]:
        out: list[EvidenceRecord] = []
        pt = _first(data.get("price_target"))
        if pt:
            out += self.targets(
                ticker,
                {
                    "low": pt.get("targetLow"),
                    "mean": pt.get("targetConsensus"),
                    "median": pt.get("targetMedian"),
                    "high": pt.get("targetHigh"),
                },
                None,
            )
        gr = _first(data.get("grades"))
        if gr:
            out += self.ratings(
                ticker,
                {
                    "strong_buy": gr.get("strongBuy"),
                    "buy": gr.get("buy"),
                    "hold": gr.get("hold"),
                    "sell": gr.get("sell"),
                    "strong_sell": gr.get("strongSell"),
                },
                None,
            )
        return out


class AlphaVantageAdapter(KeyedForecastAdapter):
    """https://www.alphavantage.co/documentation/#company-overview: the OVERVIEW
    call carries AnalystTargetPrice and the rating mix. Free keys are rate
    limited; a throttle message comes back as JSON and is reported as such."""

    name = "alphavantage"
    slug = "alphavantage"
    source_name = "Alpha Vantage"
    env_var = "ALPHAVANTAGE_API_KEY"
    page_url = "https://www.alphavantage.co/documentation/#company-overview"

    def endpoints(self, ticker: str) -> dict[str, tuple[str, dict[str, str], dict[str, str]]]:
        params = {"function": "OVERVIEW", "symbol": ticker, "apikey": self._key}
        return {"overview": ("https://www.alphavantage.co/query", params, {})}

    def parse(self, ticker: str, data: dict[str, Any]) -> list[EvidenceRecord]:
        ov = data.get("overview")
        if not isinstance(ov, dict):
            return []
        for k in ("Note", "Information", "Error Message"):
            if k in ov:
                raise_msg = str(ov[k])[:160]
                return [self._status(ticker, f"provider message: {raise_msg}")]
        return self.targets(ticker, {"mean": ov.get("AnalystTargetPrice")}, None) + self.ratings(
            ticker,
            {
                "strong_buy": ov.get("AnalystRatingStrongBuy"),
                "buy": ov.get("AnalystRatingBuy"),
                "hold": ov.get("AnalystRatingHold"),
                "sell": ov.get("AnalystRatingSell"),
                "strong_sell": ov.get("AnalystRatingStrongSell"),
            },
            None,
        )


def _first(v: Any) -> dict[str, Any] | None:
    if isinstance(v, list) and v and isinstance(v[0], dict):
        return v[0]
    return v if isinstance(v, dict) and v else None


KEYED_FORECAST_ADAPTERS: dict[str, type[KeyedForecastAdapter]] = {
    a.name: a for a in (FinnhubAdapter, FmpAdapter, AlphaVantageAdapter)
}
