from __future__ import annotations

from typing import Protocol

from ..models import EvidenceRecord


class AdapterError(Exception):
    """Raised by adapters when a provider is unreachable; callers convert this
    into an `unavailable` evidence record rather than substituting a source."""


class MarketSnapshot:
    """Raw market payload used by analytics; every number here must also be
    logged to the ledger by the adapter."""

    def __init__(
        self,
        closes: list[float],
        volumes: list[float],
        dates: list[str],
        currency: str | None,
        company_name: str | None,
        exchange: str | None,
    ) -> None:
        self.closes = closes
        self.volumes = volumes
        self.dates = dates
        self.currency = currency
        self.company_name = company_name
        self.exchange = exchange


class EvidenceAdapter(Protocol):
    name: str

    def collect(self, ticker: str) -> list[EvidenceRecord]: ...


def unavailable(
    source_name: str,
    source_type: str,
    url: str | None,
    reason: str,
    field: str | None = None,
) -> EvidenceRecord:
    from ..models import RetrievalStatus, SourceType

    return EvidenceRecord(
        claim=f"{source_name} unavailable: {reason}",
        field=field,
        source_name=source_name,
        source_url=url,
        source_type=SourceType(source_type),
        retrieval_status=RetrievalStatus.UNAVAILABLE,
        confidence=0.0,
        notes=reason,
    )
