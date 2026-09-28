"""Evidence ledger: the only place numbers are allowed to come from."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from .models import SOURCE_TIER, EvidenceRecord, RetrievalStatus, SourceType


class EvidenceLedger:
    def __init__(self) -> None:
        self._records: list[EvidenceRecord] = []
        self._by_id: dict[str, EvidenceRecord] = {}

    def add(self, record: EvidenceRecord) -> EvidenceRecord:
        if not record.id:
            record.id = f"ev-{len(self._records) + 1:03d}"
        if record.tier == 6:
            record.tier = SOURCE_TIER.get(record.source_type, 6)
        self._records.append(record)
        self._by_id[record.id] = record
        return record

    def extend(self, records: Iterable[EvidenceRecord]) -> list[EvidenceRecord]:
        return [self.add(r) for r in records]

    def get(self, evidence_id: str) -> EvidenceRecord | None:
        return self._by_id.get(evidence_id)

    def all(self) -> list[EvidenceRecord]:
        return list(self._records)

    def by_field(self, field: str) -> list[EvidenceRecord]:
        return [r for r in self._records if r.field == field]

    def by_source_type(self, source_type: SourceType) -> list[EvidenceRecord]:
        return [r for r in self._records if r.source_type == source_type]

    def best(self, field: str) -> EvidenceRecord | None:
        """Best available numeric record for a canonical field using the
        resolution order (tier), then confidence, then recency."""
        candidates = [
            r
            for r in self.by_field(field)
            if r.retrieval_status != RetrievalStatus.UNAVAILABLE and r.value is not None
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda r: (r.tier, -r.confidence, -(r.as_of.toordinal() if r.as_of else 0))
        )
        return candidates[0]

    def value(self, field: str) -> float | None:
        rec = self.best(field)
        if rec is None or not isinstance(rec.value, (int, float)):
            return None
        return float(rec.value)

    def conflicts(self, field: str, tolerance: float = 0.02) -> list[tuple[str, str, float]]:
        """Pairs of records for the same field whose values differ by more than
        `tolerance` (relative). Returned so they can be explained, not hidden."""
        recs = [r for r in self.by_field(field) if isinstance(r.value, (int, float))]
        out: list[tuple[str, str, float]] = []
        for i, a in enumerate(recs):
            for b in recs[i + 1 :]:
                av, bv = float(a.value), float(b.value)  # type: ignore[arg-type]
                denom = max(abs(av), abs(bv), 1e-9)
                diff = abs(av - bv) / denom
                if diff > tolerance:
                    out.append((a.id, b.id, diff))
        return out

    def has_citation(self, evidence_id: str) -> bool:
        return evidence_id in self._by_id

    def fingerprint(self) -> str:
        payload = json.dumps(
            [r.model_dump(mode="json", exclude={"retrieved_at"}) for r in self._records],
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]
