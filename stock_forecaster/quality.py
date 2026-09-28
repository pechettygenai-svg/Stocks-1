"""Quality gates applied to the final report before it is released."""

from __future__ import annotations

import re

from .ledger import EvidenceLedger
from .models import DISCLAIMER, AnalysisResult

PROHIBITED = [
    r"\bguaranteed?\b",
    r"\bwill reach\b",
    r"\bwill hit\b",
    r"\bcertain(ly)? to\b",
    r"\bstrong buy\b",
    r"\byou should (buy|sell|hold)\b",
    r"\bwe recommend (buying|selling|holding)\b",
    r"\bcan't lose\b",
]

CITATION_RE = re.compile(r"\[(ev-\d{3,})\]")


def run_quality_gates(result: AnalysisResult, ledger: EvidenceLedger) -> list[str]:
    failures: list[str] = []
    text = result.report_markdown or ""
    if not text:
        return ["report is empty"]
    if DISCLAIMER not in text:
        failures.append("disclaimer missing")
    if text.find(DISCLAIMER) > 600:
        failures.append("disclaimer not in first visible section")
    for pat in PROHIBITED:
        if re.search(pat, text, flags=re.IGNORECASE):
            failures.append(f"prohibited certainty/recommendation language: /{pat}/")
    cited = set(CITATION_RE.findall(text))
    for cid in cited:
        if not ledger.has_citation(cid):
            failures.append(f"citation {cid} does not exist in ledger")
    if len(cited) < 3:
        failures.append("fewer than 3 evidence citations in report")
    names = {s.name for s in result.scenarios}
    if not {"Bear", "Base", "Bull"} <= names:
        failures.append("bull/base/bear scenarios missing")
    for s in result.scenarios:
        if not s.what_must_be_true:
            failures.append(f"{s.name} scenario has no measurable conditions")
    if "## Sources" not in text:
        failures.append("sources section missing")
    if "## Limitations" not in text:
        failures.append("limitations section missing")
    for f in ("As of:", "Horizon:"):
        if f not in text:
            failures.append(f"header missing '{f}'")
    return failures
