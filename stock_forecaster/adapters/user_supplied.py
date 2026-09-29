"""Forecast values the user transcribed from a page this tool may not fetch
(Fidelity, MSN, ...). They enter the ledger as tier-5 source opinions marked
`snippet_only`, never as verified data."""

from __future__ import annotations

from ..models import EvidenceRecord, RetrievalStatus, SourceType, UserForecast

SOURCE_TYPE_BY_NAME = {"fidelity": SourceType.BROKER, "msn": SourceType.FORECAST}


class UserForecastAdapter:
    name = "user_forecasts"

    def __init__(self, forecasts: list[UserForecast]):
        self._forecasts = forecasts

    def collect(self, ticker: str) -> list[EvidenceRecord]:
        records: list[EvidenceRecord] = []
        for i, f in enumerate(self._forecasts):
            stype = SOURCE_TYPE_BY_NAME.get(f.source.lower().split()[0], SourceType.FORECAST)
            for kind, val in (("low", f.value_low), ("mean", f.value_mean), ("high", f.value_high)):
                if val is None:
                    continue
                records.append(
                    EvidenceRecord(
                        claim=f"{f.source} {f.forecast_type} ({kind}): {val:,.2f} (user-supplied)",
                        field=f"user_forecast_{i}_{kind}",
                        value=float(val),
                        unit="currency",
                        as_of=f.as_of,
                        period=f.horizon,
                        definition=f.forecast_type,
                        source_name=f"{f.source} (user-supplied)",
                        source_url=f.url,
                        source_type=stype,
                        tier=5,
                        retrieval_status=RetrievalStatus.SNIPPET_ONLY,
                        confidence=0.3,
                        notes=(
                            f"Transcribed by user for {ticker}; not fetched by this tool. "
                            f"analysts={f.analyst_count}, method_disclosed={f.method_disclosed}"
                        ),
                    )
                )
        return records
