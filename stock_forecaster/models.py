"""Core data contracts shared by adapters, analytics, LLM roles and the report."""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

DISCLAIMER = (
    "This is informational research, not investment advice or a recommendation "
    "to buy, sell, or hold a security."
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SourceType(str, Enum):
    FILING = "filing"
    COMPANY = "company"
    MARKET_DATA = "market_data"
    BROKER = "broker"
    FORECAST = "forecast"
    NEWS = "news"
    CALCULATION = "calculation"


class RetrievalStatus(str, Enum):
    VERIFIED = "verified"
    SNIPPET_ONLY = "snippet_only"
    UNAVAILABLE = "unavailable"


# Resolution order for reported financials (lower tier wins).
SOURCE_TIER: dict[SourceType, int] = {
    SourceType.FILING: 1,
    SourceType.COMPANY: 2,
    SourceType.MARKET_DATA: 3,
    SourceType.BROKER: 4,
    SourceType.FORECAST: 5,
    SourceType.NEWS: 6,
    SourceType.CALCULATION: 3,
}


class EvidenceRecord(BaseModel):
    """One normalized observation. A source can support a number without
    supporting the conclusion drawn from it."""

    id: str = ""
    claim: str
    field: str | None = None
    value: float | str | None = None
    unit: str | None = None
    currency: str | None = None
    as_of: date | None = None
    published_at: date | None = None
    period: str | None = None
    definition: str | None = None
    source_name: str
    source_url: str | None = None
    source_type: SourceType
    tier: int = 6
    retrieval_status: RetrievalStatus = RetrievalStatus.VERIFIED
    calculation: str | None = None
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    supports: list[str] = Field(default_factory=list)
    contradicts: list[str] = Field(default_factory=list)
    derived_from: list[str] = Field(default_factory=list)
    notes: str | None = None
    retrieved_at: datetime = Field(default_factory=utc_now)

    def cite(self) -> str:
        return f"[{self.id}]"


class FindingType(str, Enum):
    FACT = "fact"
    SOURCE_OPINION = "source_opinion"
    MODEL_OUTPUT = "model_output"
    INFERENCE = "inference"


class Finding(BaseModel):
    statement: str
    type: FindingType
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    caveat: str | None = None


class RoleOutput(BaseModel):
    role: str
    findings: list[Finding] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    invalidated_if: list[str] = Field(default_factory=list)
    model: str | None = None


class CalculationResult(BaseModel):
    name: str
    formula_version: str
    inputs: dict[str, Any]
    outputs: dict[str, Any]
    warnings: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class ScenarioDrivers(BaseModel):
    revenue_growth: float
    operating_margin: float
    share_change: float = 0.0
    exit_multiple: float
    label: str


class Scenario(BaseModel):
    name: str
    drivers: ScenarioDrivers
    implied_price_low: float | None
    implied_price_high: float | None
    implied_price_mid: float | None
    probability: float
    what_must_be_true: list[str]
    warnings: list[str] = Field(default_factory=list)


class ForecastComparison(BaseModel):
    source: str
    forecast_type: str
    horizon: str
    value_low: float | None = None
    value_mean: float | None = None
    value_high: float | None = None
    as_of: date | None = None
    analyst_count: int | None = None
    method_disclosed: bool = False
    weight: str = "context"
    status: RetrievalStatus = RetrievalStatus.VERIFIED
    url: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class Horizon(str, Enum):
    M12 = "12m"
    Y3 = "3y"
    Y5 = "5y"

    @property
    def years(self) -> float:
        return {"12m": 1.0, "3y": 3.0, "5y": 5.0}[self.value]


class AnalysisRequest(BaseModel):
    ticker: str
    exchange: str | None = None
    horizon: Horizon = Horizon.M12
    depth: str = "standard"
    include_sources: list[str] = Field(default_factory=lambda: ["yahoo", "sec", "fidelity", "msn"])
    risk_tolerance: str | None = None


class RunStatus(str, Enum):
    QUEUED = "queued"
    COLLECTING = "collecting"
    VALIDATING = "validating"
    MODELING = "modeling"
    REVIEWING = "reviewing"
    COMPLETE = "complete"
    FAILED = "failed"


class AnalysisResult(BaseModel):
    run_id: str
    request: AnalysisRequest
    status: RunStatus
    started_at: datetime
    finished_at: datetime | None = None
    company_name: str | None = None
    currency: str | None = None
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    calculations: list[CalculationResult] = Field(default_factory=list)
    scenarios: list[Scenario] = Field(default_factory=list)
    sensitivity: dict[str, Any] = Field(default_factory=dict)
    forecasts: list[ForecastComparison] = Field(default_factory=list)
    role_outputs: list[RoleOutput] = Field(default_factory=list)
    validation_warnings: list[str] = Field(default_factory=list)
    quality_gate_failures: list[str] = Field(default_factory=list)
    report_markdown: str | None = None
    error: str | None = None
