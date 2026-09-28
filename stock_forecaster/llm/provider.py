"""Provider-neutral LLM interface. Selected via LLM_PROVIDER=openai|anthropic|none
and LLM_MODEL. Keys are read from OPENAI_API_KEY / ANTHROPIC_API_KEY and never
logged or placed in prompts."""

from __future__ import annotations

import json
import os
from typing import Any, Protocol

from ..models import EvidenceRecord


class LLMError(Exception):
    pass


class LLMProvider(Protocol):
    name: str
    model: str

    def structured(
        self,
        task: str,
        evidence: list[EvidenceRecord],
        schema: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def text(
        self,
        task: str,
        evidence: list[EvidenceRecord],
        citations: list[str],
        context: dict[str, Any] | None = None,
    ) -> str: ...


def _evidence_bundle(evidence: list[EvidenceRecord]) -> str:
    compact = [
        {
            "id": r.id,
            "claim": r.claim,
            "field": r.field,
            "value": r.value,
            "unit": r.unit,
            "as_of": str(r.as_of) if r.as_of else None,
            "period": r.period,
            "source": r.source_name,
            "type": r.source_type.value,
            "tier": r.tier,
            "status": r.retrieval_status.value,
            "confidence": r.confidence,
        }
        for r in evidence
    ]
    return json.dumps(compact, separators=(",", ":"))


SYSTEM_RULES = (
    "You are a bounded research analyst. Rules: use ONLY the evidence records and computed "
    "outputs provided; never introduce a price, metric, target, quote or source not present. "
    "Cite evidence ids like [ev-001]. Label each statement as fact, source_opinion, model_output "
    "or inference. Never use certainty or recommendation language (guaranteed, will reach, buy, "
    "sell, hold). This is informational research, not investment advice."
)


def build_prompt(
    task: str,
    evidence: list[EvidenceRecord],
    context: dict[str, Any] | None,
    schema: dict[str, Any] | None,
) -> str:
    parts = [f"TASK:\n{task}", f"EVIDENCE (JSON records):\n{_evidence_bundle(evidence)}"]
    if context:
        parts.append(f"COMPUTED OUTPUTS / CONTEXT (JSON):\n{json.dumps(context, default=str)}")
    if schema:
        parts.append(
            "Respond with ONLY a JSON object matching this JSON Schema:\n" + json.dumps(schema)
        )
    return "\n\n".join(parts)


def _parse_json(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[raw.find("{") :]
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        raise LLMError("model did not return JSON")
    obj = json.loads(raw[start : end + 1])
    if not isinstance(obj, dict):
        raise LLMError("model JSON is not an object")
    return obj


class OpenAIProvider:
    name = "openai"

    def __init__(self, model: str | None = None):
        try:
            from openai import OpenAI  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            raise LLMError("install with: pip install 'stock-forecaster[openai]'") from exc
        if not os.environ.get("OPENAI_API_KEY"):
            raise LLMError("OPENAI_API_KEY not set")
        self._client = OpenAI()
        self.model = model or os.environ.get("LLM_MODEL", "gpt-4o-mini")

    def _call(self, prompt: str, temperature: float, json_mode: bool) -> str:
        resp = self._client.responses.create(
            model=self.model,
            instructions=SYSTEM_RULES,
            input=prompt,
            temperature=temperature,
            max_output_tokens=2500,
            **({"text": {"format": {"type": "json_object"}}} if json_mode else {}),
        )
        return str(resp.output_text)

    def structured(
        self,
        task: str,
        evidence: list[EvidenceRecord],
        schema: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        prompt = build_prompt(task, evidence, context, schema)
        try:
            return _parse_json(self._call(prompt, 0.1, True))
        except (LLMError, json.JSONDecodeError):
            repair = prompt + "\n\nYour previous reply was not valid JSON. Return only valid JSON."
            return _parse_json(self._call(repair, 0.0, True))

    def text(
        self,
        task: str,
        evidence: list[EvidenceRecord],
        citations: list[str],
        context: dict[str, Any] | None = None,
    ) -> str:
        prompt = build_prompt(task, evidence, context, None)
        prompt += f"\n\nAllowed citations: {', '.join(citations)}"
        return self._call(prompt, 0.3, False)


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str | None = None):
        try:
            import anthropic  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            raise LLMError("install with: pip install 'stock-forecaster[anthropic]'") from exc
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise LLMError("ANTHROPIC_API_KEY not set")
        self._client = anthropic.Anthropic()
        self.model = model or os.environ.get("LLM_MODEL", "claude-3-5-sonnet-latest")

    def _call(self, prompt: str, temperature: float) -> str:
        msg = self._client.messages.create(
            model=self.model,
            system=SYSTEM_RULES,
            max_tokens=2500,
            temperature=temperature,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(getattr(b, "text", "") for b in msg.content)

    def structured(
        self,
        task: str,
        evidence: list[EvidenceRecord],
        schema: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        prompt = build_prompt(task, evidence, context, schema)
        try:
            return _parse_json(self._call(prompt, 0.1))
        except (LLMError, json.JSONDecodeError):
            repair = prompt + "\n\nYour previous reply was not valid JSON. Return only valid JSON."
            return _parse_json(self._call(repair, 0.0))

    def text(
        self,
        task: str,
        evidence: list[EvidenceRecord],
        citations: list[str],
        context: dict[str, Any] | None = None,
    ) -> str:
        prompt = build_prompt(task, evidence, context, None)
        prompt += f"\n\nAllowed citations: {', '.join(citations)}"
        return self._call(prompt, 0.3)


class NullProvider:
    """No model. Roles fall back to deterministic templates; report uses
    computed outputs only. Useful for tests and offline runs."""

    name = "none"
    model = "deterministic"

    def structured(
        self,
        task: str,
        evidence: list[EvidenceRecord],
        schema: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raise LLMError("no LLM provider configured")

    def text(
        self,
        task: str,
        evidence: list[EvidenceRecord],
        citations: list[str],
        context: dict[str, Any] | None = None,
    ) -> str:
        raise LLMError("no LLM provider configured")


def get_provider(name: str | None = None, model: str | None = None) -> LLMProvider:
    name = (name or os.environ.get("LLM_PROVIDER", "none")).lower()
    if name == "openai":
        return OpenAIProvider(model)
    if name == "anthropic":
        return AnthropicProvider(model)
    if name in ("none", "", "null"):
        return NullProvider()
    raise LLMError(f"unknown LLM_PROVIDER {name!r}")
