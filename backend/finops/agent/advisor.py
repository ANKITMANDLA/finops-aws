"""Turn a scan summary into architectural recommendations.

The LLM is the last mile, not the source of truth. If it is not configured, errors, or
returns something unparseable, we fall back to a deterministic narrative assembled from
the findings so the Architecture view is never empty.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence

from finops.agent.prompts import SYSTEM_PROMPT, build_user_prompt
from finops.agent.provider import LlmProvider, ProviderError, build_provider
from finops.config import Settings
from finops.model import (
    Advice,
    ArchitectureRecommendation,
    CapabilityNote,
    Finding,
    Resource,
    TcoReport,
)
from finops.tco import rank_findings, summarize_for_advisor
from finops.util import round_money

logger = logging.getLogger(__name__)

MAX_RECOMMENDATIONS = 6
_EFFORTS = {"low", "medium", "high"}
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


class Advisor:
    """Wraps a provider with prompt construction, parsing, and a safety net."""

    def __init__(self, provider: LlmProvider, *, max_findings: int = 40) -> None:
        self.provider = provider
        self.max_findings = max_findings

    def advise(
        self,
        report: TcoReport,
        findings: Sequence[Finding],
        resources: Sequence[Resource],
        notes: Sequence[CapabilityNote] | None = None,
    ) -> Advice:
        summary = summarize_for_advisor(report, findings, resources, max_findings=self.max_findings)
        gap_notes = [
            f"{note.capability}: {note.message}" for note in (notes or []) if note.status != "ok"
        ]

        if self.provider.name == "none":
            return _fallback_advice(report, findings, reason=None)

        try:
            raw = self.provider.complete(SYSTEM_PROMPT, build_user_prompt(summary, gap_notes))
        except ProviderError as exc:
            logger.warning("LLM advisor unavailable: %s", exc)
            return _fallback_advice(report, findings, reason=str(exc))

        try:
            advice = _parse_advice(raw, findings)
        except ValueError as exc:
            logger.warning("LLM advisor returned unusable output: %s", exc)
            return _fallback_advice(report, findings, reason=f"Could not parse response: {exc}")

        advice.provider = self.provider.name
        advice.model = self.provider.model
        return advice


def build_advisor(settings: Settings, aws=None) -> Advisor:
    return Advisor(build_provider(settings, aws))


def _parse_advice(raw: str, findings: Sequence[Finding]) -> Advice:
    """Parse the model's JSON, tolerating code fences and surrounding chatter."""
    payload = _extract_json(raw)
    known_ids = {finding.id for finding in findings}

    recommendations = []
    for item in payload.get("recommendations", [])[:MAX_RECOMMENDATIONS]:
        if not isinstance(item, dict):
            continue
        title = _text(item.get("title"))
        summary = _text(item.get("summary"))
        if not title or not summary:
            continue
        recommendations.append(
            ArchitectureRecommendation(
                title=title,
                summary=summary,
                rationale=_text(item.get("rationale")),
                affected_services=_string_list(item.get("affected_services")),
                estimated_monthly_savings=_savings(item.get("estimated_monthly_savings")),
                implementation_effort=_level(item.get("implementation_effort")),
                risk=_level(item.get("risk")),
                steps=_string_list(item.get("steps")),
                # Drop hallucinated ids so the UI never links to a finding that is not there.
                related_finding_ids=[
                    ref for ref in _string_list(item.get("related_finding_ids")) if ref in known_ids
                ],
                tradeoffs=_text(item.get("tradeoffs")) or None,
            )
        )

    return Advice(
        executive_summary=_text(payload.get("executive_summary")),
        recommendations=recommendations,
        quick_wins=_string_list(payload.get("quick_wins"))[:8],
        caveats=_string_list(payload.get("caveats"))[:8],
    )


def _extract_json(raw: str) -> dict:
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty response")
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_OBJECT.search(text)
        if not match:
            raise ValueError("no JSON object in response") from None
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise ValueError(str(exc)) from exc
    if not isinstance(payload, dict):
        raise ValueError("expected a JSON object")
    return payload


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _string_list(value) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _level(value) -> str:
    text = value.lower().strip() if isinstance(value, str) else ""
    return text if text in _EFFORTS else "medium"


def _savings(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return round_money(float(value)) if value > 0 else None
    if isinstance(value, str):
        cleaned = value.replace("$", "").replace(",", "").strip()
        try:
            return _savings(float(cleaned))
        except ValueError:
            return None
    return None


def _fallback_advice(
    report: TcoReport, findings: Sequence[Finding], *, reason: str | None
) -> Advice:
    """Deterministic narrative used when no model answered.

    It says less than a model would, but everything it says is arithmetic on numbers we
    already computed, so it is safe to show.
    """
    ranked = rank_findings(findings)
    top_service = report.by_service[0].key if report.by_service else "your workloads"
    top_categories = [item.key for item in report.by_category[:3]]

    if report.monthly_run_rate > 0:
        summary = (
            f"This account runs at about ${report.monthly_run_rate:,.0f} per month, led by "
            f"{top_service}. Deterministic rules identified "
            f"${report.identified_monthly_savings:,.0f} per month of addressable waste "
            f"({report.savings_percent:.1f}% of spend) across "
            f"{len(findings)} findings."
        )
        if top_categories:
            summary += (
                " The largest opportunities are in " + ", ".join(top_categories).lower() + "."
            )
    elif findings:
        # No run rate but plenty of findings means Cost Explorer was unavailable, not that
        # the account is free. Report the waste and be explicit about the missing baseline.
        summary = (
            f"Cost Explorer returned no billed cost for this period, so there is no spend "
            f"baseline to compare against. The inventory itself still yields "
            f"${sum(f.estimated_monthly_savings for f in findings):,.0f} per month of "
            f"addressable waste across {len(findings)} findings, priced from list rates and "
            f"AWS's own recommendations."
        )
        if top_categories:
            summary += (
                " The largest opportunities are in " + ", ".join(top_categories).lower() + "."
            )
    else:
        summary = (
            "No billed cost was returned for this period, so there is nothing to optimize yet."
        )

    caveats = [
        "Architectural narrative is unavailable, so this summary is generated directly from "
        "the findings rather than by a model."
    ]
    if reason:
        caveats.append(f"LLM advisor error: {reason}")
    else:
        caveats.append(
            "Set FINOPS_LLM_PROVIDER (bedrock, anthropic, or openai) to enable architectural "
            "recommendations."
        )

    return Advice(
        provider="none",
        executive_summary=summary,
        quick_wins=[
            f"{finding.title} (~${finding.estimated_monthly_savings:,.0f}/mo)"
            for finding in ranked
            if finding.implementation_effort == "low" and finding.estimated_monthly_savings > 0
        ][:5],
        caveats=caveats,
        error=reason,
    )
