"""Critic + auditor pass over the draft. Deterministic checks run always; when a
provider is configured its findings are appended (citations verified). The
synthesizer revises the draft from the resulting findings."""

from __future__ import annotations

import re
from typing import Any

from .ledger import EvidenceLedger
from .llm.provider import LLMError, LLMProvider
from .models import (
    AnalysisResult,
    FindingType,
    ReviewFinding,
    ReviewSeverity,
    RoleOutput,
)

NUMBER_RE = re.compile(r"(?<![\w\[-])[-+]?\$?\d[\d,]*\.?\d*\s?(%|x|bn|m|USD|EUR|GBP)?(?![\w\]-])")
CITATION_RE = re.compile(r"\[ev-\d{3,}\]")
OPINION_WORDS = ("consensus", "estimate", "target", "analyst", "forecast", "expects", "projected")
BULL_WORDS = ("upside", "growth", "strong", "expand", "accelerat", "beat", "outperform")
AUDITED_SECTIONS = {
    "Executive view",
    "Current snapshot",
    "What the company does",
    "Fundamentals and valuation",
    "Technical context",
    "External forecast comparison",
    "Catalysts and risks",
}
BEAR_WORDS = ("downside", "risk", "decline", "compress", "slow", "miss", "underperform", "dilut")

REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "check": {"type": "string"},
                    "severity": {"enum": ["info", "warning", "blocking"]},
                    "message": {"type": "string"},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["check", "severity", "message"],
            },
        }
    },
    "required": ["findings"],
}

CRITIC_TASK = (
    "You are the skeptical critic. Read the draft report and role findings. Flag "
    "confirmation bias, a generic or missing bear case, bull claims without measurable "
    "conditions, and any conclusion stronger than the evidence supports. Do not add facts."
)
AUDITOR_TASK = (
    "You are the forecast/citation auditor. Check every number in the draft has a citation "
    "on the same line or an as-of date, that source opinions are not stated as facts, that "
    "external forecasts carry age and methodology, and that conflicting values are shown "
    "side by side. Do not add facts."
)


def _f(
    reviewer: str,
    check: str,
    sev: ReviewSeverity,
    msg: str,
    ids: list[str] | None = None,
) -> ReviewFinding:
    return ReviewFinding(
        reviewer=reviewer, check=check, severity=sev, message=msg, evidence_ids=ids or []
    )


def deterministic_review(result: AnalysisResult, ledger: EvidenceLedger) -> list[ReviewFinding]:
    out: list[ReviewFinding] = []
    text = result.report_markdown or ""

    # ---- auditor: numbers without citations in prose lines ------------------
    uncited: list[str] = []
    section, in_calc_block = "", False
    for line in text.splitlines():
        if line.startswith("## "):
            section, in_calc_block = line[3:].split(" (")[0], False
            continue
        if not line.strip():
            in_calc_block = False
            continue
        if "(formula " in line:
            in_calc_block = True  # computed outputs carry a formula version, not a citation
            continue
        if section not in AUDITED_SECTIONS or in_calc_block:
            continue
        if line.startswith(("|", ">", "**As of", "- ⚠")):
            continue
        if NUMBER_RE.search(line) and not CITATION_RE.search(line):
            uncited.append(line.strip()[:70])
    if uncited:
        out.append(
            _f(
                "auditor",
                "uncited_numbers",
                ReviewSeverity.WARNING,
                f"{len(uncited)} prose line(s) contain numbers without an [ev-NNN] citation, "
                f"e.g. '{uncited[0]}'.",
            )
        )

    # ---- auditor: source opinions phrased as facts ---------------------------
    for ro in result.role_outputs:
        for fnd in ro.findings:
            if fnd.type == FindingType.SOURCE_OPINION and not any(
                w in fnd.statement.lower() for w in OPINION_WORDS
            ):
                out.append(
                    _f(
                        "auditor",
                        "opinion_as_fact",
                        ReviewSeverity.WARNING,
                        f"{ro.role}: source opinion lacks attribution wording: "
                        f"'{fnd.statement[:80]}'",
                        fnd.evidence_ids,
                    )
                )

    # ---- auditor: forecast panel hygiene -------------------------------------
    for p in result.forecasts:
        if p.duplicate_of:
            out.append(
                _f(
                    "auditor",
                    "duplicate_forecast",
                    ReviewSeverity.INFO,
                    f"{p.source} duplicates {p.duplicate_of}; weight set to 'duplicate'.",
                    p.evidence_ids,
                )
            )
        if p.stale:
            out.append(
                _f(
                    "auditor",
                    "stale_forecast",
                    ReviewSeverity.WARNING,
                    f"{p.source} forecast dated {p.as_of} is stale for a {p.horizon} horizon.",
                    p.evidence_ids,
                )
            )
        if p.outlier:
            out.append(
                _f(
                    "auditor",
                    "outlier_forecast",
                    ReviewSeverity.INFO,
                    f"{p.source} mean is an outlier versus the panel; shown, not averaged.",
                    p.evidence_ids,
                )
            )
        if p.value_mean is not None and not p.method_disclosed:
            out.append(
                _f(
                    "auditor",
                    "opaque_method",
                    ReviewSeverity.INFO,
                    f"{p.source}: methodology not disclosed; treated as context only.",
                    p.evidence_ids,
                )
            )

    # ---- auditor: unresolved conflicts ---------------------------------------
    for field in {r.field for r in ledger.all() if r.field}:
        if ledger.conflicts(field) and "conflict" not in text.lower():
            out.append(
                _f(
                    "auditor",
                    "conflict_not_explained",
                    ReviewSeverity.WARNING,
                    f"field '{field}' has conflicting values that the draft does not discuss.",
                    [r.id for r in ledger.by_field(field)],
                )
            )

    # ---- critic: bear case quality --------------------------------------------
    bear = next((s for s in result.scenarios if s.name == "Bear"), None)
    if result.scenarios and (bear is None or not bear.what_must_be_true):
        out.append(
            _f(
                "critic",
                "bear_case_missing",
                ReviewSeverity.BLOCKING,
                "Bear scenario missing or has no measurable conditions.",
            )
        )
    bull = next((s for s in result.scenarios if s.name == "Bull"), None)
    if bull and not any(re.search(r"\d", c) for c in bull.what_must_be_true):
        out.append(
            _f(
                "critic",
                "bull_not_measurable",
                ReviewSeverity.WARNING,
                "Bull scenario conditions contain no measurable quantities.",
            )
        )

    # ---- critic: bull/bear balance ----------------------------------------------
    low = text.lower()
    bulls = sum(low.count(w) for w in BULL_WORDS)
    bears = sum(low.count(w) for w in BEAR_WORDS)
    if bulls > 2 * max(bears, 1):
        out.append(
            _f(
                "critic",
                "bull_bias",
                ReviewSeverity.WARNING,
                f"Draft language skews bullish ({bulls} bullish vs {bears} bearish terms); "
                "the revision must expand the downside discussion.",
            )
        )

    # ---- critic: confidence vs evidence -------------------------------------------
    unavailable = [r for r in ledger.all() if r.retrieval_status.value == "unavailable"]
    if "high confidence" in low and len(unavailable) >= 2:
        out.append(
            _f(
                "critic",
                "overconfident",
                ReviewSeverity.BLOCKING,
                "Draft claims high confidence while two or more sources are unavailable.",
                [r.id for r in unavailable],
            )
        )
    return out


def model_review(
    result: AnalysisResult, ledger: EvidenceLedger, provider: LLMProvider
) -> list[ReviewFinding]:
    if provider.name == "none":
        return []
    out: list[ReviewFinding] = []
    context = {
        "draft_report": (result.report_markdown or "")[:12000],
        "role_outputs": [ro.model_dump(mode="json") for ro in result.role_outputs],
        "forecast_panel": [p.model_dump(mode="json") for p in result.forecasts],
    }
    for reviewer, task in (("critic", CRITIC_TASK), ("auditor", AUDITOR_TASK)):
        try:
            raw = provider.structured(task, ledger.all(), REVIEW_SCHEMA, context)
        except (LLMError, Exception):
            out.append(
                _f(
                    reviewer,
                    "model_review_unavailable",
                    ReviewSeverity.INFO,
                    f"{reviewer} model review discarded (schema/provider failure).",
                )
            )
            continue
        for f in raw.get("findings", []):
            ids = [i for i in f.get("evidence_ids", []) if ledger.has_citation(i)]
            try:
                sev = ReviewSeverity(f.get("severity", "info"))
            except ValueError:
                sev = ReviewSeverity.INFO
            out.append(
                _f(reviewer, str(f.get("check", "model")), sev, str(f.get("message", "")), ids)
            )
    return out


def apply_review(result: AnalysisResult, findings: list[ReviewFinding]) -> list[RoleOutput]:
    """Resolve what can be resolved mechanically and return the role outputs the
    synthesizer should use for the revision."""
    roles = [ro.model_copy(deep=True) for ro in result.role_outputs]
    for f in findings:
        if f.check == "opinion_as_fact":
            for ro in roles:
                for fnd in ro.findings:
                    if (
                        fnd.evidence_ids == f.evidence_ids
                        and fnd.type == FindingType.SOURCE_OPINION
                    ):
                        fnd.statement = f"Per source (opinion, not verified fact): {fnd.statement}"
            f.resolution = "attribution wording added"
        elif f.check in (
            "duplicate_forecast",
            "outlier_forecast",
            "stale_forecast",
            "opaque_method",
        ):
            f.resolution = "flagged in External forecast comparison table"
        elif f.check == "bull_bias":
            skeptic = next((ro for ro in roles if ro.role == "skeptic"), None)
            if skeptic:
                skeptic.open_questions.append(
                    "Critic: bullish framing exceeded bearish framing in the draft; "
                    "downside conditions were expanded in revision."
                )
            f.resolution = "skeptic open question added; Review notes section lists the concern"
        elif f.check == "uncited_numbers":
            f.resolution = "listed in Review notes; numbers are computed outputs unless cited"
        elif f.check == "conflict_not_explained":
            f.resolution = "conflict listed in Review notes with both values"
        elif f.severity == ReviewSeverity.BLOCKING:
            f.resolution = None  # left open; surfaces in Limitations and gates
        else:
            f.resolution = "noted"
    return roles
