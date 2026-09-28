"""Cross-check sources without a permitted structured feed (Fidelity, MSN).

These adapters never scrape. They register the public page URL as an
`unavailable` cross-check record so the report can list the source and the
user can open it, without any fabricated or scraped value.
"""

from __future__ import annotations

from ..models import EvidenceRecord, RetrievalStatus, SourceType


class LinkOnlyAdapter:
    def __init__(self, name: str, source_name: str, url_template: str, source_type: SourceType):
        self.name = name
        self.source_name = source_name
        self.url_template = url_template
        self.source_type = source_type

    def collect(self, ticker: str) -> list[EvidenceRecord]:
        return [
            EvidenceRecord(
                claim=f"{self.source_name} quote/research page (manual cross-check; no permitted "
                "structured feed, values not retrieved)",
                field="cross_check_link",
                source_name=self.source_name,
                source_url=self.url_template.format(ticker=ticker),
                source_type=self.source_type,
                retrieval_status=RetrievalStatus.UNAVAILABLE,
                confidence=0.0,
                notes="Not scraped; terms-of-use/robots respected.",
            )
        ]


def fidelity_adapter() -> LinkOnlyAdapter:
    return LinkOnlyAdapter(
        "fidelity",
        "Fidelity",
        "https://digital.fidelity.com/prgw/digital/research/quote/dashboard/summary?symbol={ticker}",
        SourceType.BROKER,
    )


def msn_adapter() -> LinkOnlyAdapter:
    return LinkOnlyAdapter(
        "msn",
        "MSN Money",
        "https://www.msn.com/en-us/money/stockdetails?symbol={ticker}",
        SourceType.FORECAST,
    )
