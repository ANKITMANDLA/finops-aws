"""Rule contract, evaluation context, and the de-duplicating merge.

A rule turns observed facts (inventory, utilization, cost) into a
:class:`~finops.model.Finding` with a dollar figure and the evidence behind it. Rules
never call AWS; everything they need is already in the :class:`RuleContext`, which makes
them fast, deterministic, and trivial to test against fixtures.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from functools import cached_property
from typing import ClassVar

from finops.aws.costs import CostSnapshot
from finops.aws.pricing import PricingClient
from finops.config import Thresholds
from finops.model import (
    Evidence,
    Finding,
    FindingCategory,
    Remediation,
    Resource,
    make_finding_id,
    utcnow,
)
from finops.util import days_since

logger = logging.getLogger(__name__)

# When two sources report the same action on the same resource, the one earlier in this
# list supplies the savings figure. AWS's own estimates use billing data we cannot see,
# so they outrank our list-price arithmetic.
SOURCE_PRIORITY = (
    "cost-optimization-hub",
    "compute-optimizer",
    "trusted-advisor",
    "cost-explorer",
    "rules",
)


@dataclass
class RuleContext:
    """Everything the rules need, pre-indexed."""

    resources: list[Resource]
    cost: CostSnapshot
    pricing: PricingClient
    thresholds: Thresholds = field(default_factory=Thresholds)
    now: datetime = field(default_factory=utcnow)

    @cached_property
    def by_type(self) -> dict[str, list[Resource]]:
        grouped: dict[str, list[Resource]] = {}
        for resource in self.resources:
            grouped.setdefault(resource.resource_type, []).append(resource)
        return grouped

    @cached_property
    def by_arn(self) -> dict[str, Resource]:
        return {resource.arn: resource for resource in self.resources}

    @cached_property
    def by_id(self) -> dict[str, Resource]:
        return {resource.resource_id: resource for resource in self.resources}

    def of_type(self, *resource_types: str) -> list[Resource]:
        return [item for kind in resource_types for item in self.by_type.get(kind, [])]

    @cached_property
    def image_ids_in_use(self) -> set[str]:
        """AMIs referenced by an existing instance, which must not be deleted."""
        return {
            instance.attributes.get("image_id")
            for instance in self.of_type("ec2:instance")
            if instance.attributes.get("image_id")
        }  # type: ignore[return-value]

    @cached_property
    def volume_ids_present(self) -> set[str]:
        return {volume.resource_id for volume in self.of_type("ebs:volume")}

    @cached_property
    def snapshot_ids_backing_images(self) -> set[str]:
        """Snapshots owned by an AMI; deleting them directly would break the image."""
        backing: set[str] = set()
        for image in self.of_type("ec2:image"):
            backing.update(image.attributes.get("snapshot_ids") or [])
        return backing

    def age_days(self, resource: Resource) -> float | None:
        return days_since(resource.created_at, now=self.now)

    @staticmethod
    def monthly_cost(resource: Resource) -> float:
        return resource.monthly_cost or 0.0


class Rule(ABC):
    """One cost reduction check."""

    id: ClassVar[str]
    category: ClassVar[FindingCategory]
    title: ClassVar[str] = ""

    @abstractmethod
    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        """Yield a finding for every resource that trips this rule."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Rule {self.id}>"


REGISTRY: dict[str, type[Rule]] = {}


def register(cls: type[Rule]) -> type[Rule]:
    if not getattr(cls, "id", None):
        raise ValueError(f"{cls.__name__} must define an 'id'")
    if cls.id in REGISTRY:
        raise ValueError(f"Duplicate rule id: {cls.id}")
    REGISTRY[cls.id] = cls
    return cls


def build_rules(only: Sequence[str] | None = None, skip: Sequence[str] | None = None) -> list[Rule]:
    ids = list(only) if only else sorted(REGISTRY)
    unknown = [rule_id for rule_id in ids if rule_id not in REGISTRY]
    if unknown:
        raise ValueError(f"Unknown rule(s): {', '.join(unknown)}")
    if skip:
        ids = [rule_id for rule_id in ids if rule_id not in set(skip)]
    return [REGISTRY[rule_id]() for rule_id in ids]


def run_rules(
    ctx: RuleContext, only: Sequence[str] | None = None, skip: Sequence[str] | None = None
) -> list[Finding]:
    """Evaluate every rule. A rule that raises is logged and skipped, not fatal."""
    findings: list[Finding] = []
    for rule in build_rules(only, skip):
        try:
            produced = list(rule.evaluate(ctx))
        except Exception:  # noqa: BLE001 - one broken rule must not lose the others
            logger.exception("Rule %s failed", rule.id)
            continue
        findings.extend(produced)
        if produced:
            logger.debug("Rule %s produced %d findings", rule.id, len(produced))
    return findings


def finding_for(
    resource: Resource,
    *,
    rule_id: str,
    title: str,
    category: FindingCategory,
    action: str,
    savings: float,
    detail: str,
    evidence: Sequence[Evidence] = (),
    remediation: Remediation | None = None,
    confidence: str = "medium",
    effort: str = "low",
    risk: str = "low",
    cost_basis: str | None = None,
    rollback_possible: bool = True,
) -> Finding:
    """Build a finding from a resource, carrying over its identifying fields."""
    return Finding(
        id=make_finding_id(action, resource.arn),
        rule_id=rule_id,
        title=title,
        category=category,
        action_type=action,
        service=resource.service,
        source="rules",
        resource_arn=resource.arn,
        resource_id=resource.resource_id,
        resource_type=resource.resource_type,
        region=resource.region,
        estimated_monthly_savings=round(max(savings, 0.0), 2),
        confidence=confidence,  # type: ignore[arg-type]
        implementation_effort=effort,  # type: ignore[arg-type]
        risk=risk,  # type: ignore[arg-type]
        # Savings inherit the basis of the cost they are derived from.
        cost_basis=cost_basis or resource.cost_basis or "heuristic",  # type: ignore[arg-type]
        rollback_possible=rollback_possible,
        detail=detail,
        evidence=list(evidence),
        remediation=remediation,
        tags=dict(resource.tags),
    )


# ------------------------------------------------------------------ merging


def merge_findings(findings: Iterable[Finding], *, min_savings: float = 0.0) -> list[Finding]:
    """Collapse duplicate findings and drop the ones too small to act on.

    Two findings collide when they propose the same action on the same resource. That
    happens routinely: our gp2-to-gp3 rule and Compute Optimizer's volume
    recommendation describe the same change. Counting both would inflate the total
    savings, which is the number people plan against.
    """
    merged: dict[str, Finding] = {}
    for finding in findings:
        existing = merged.get(finding.id)
        merged[finding.id] = _merge_pair(existing, finding) if existing else finding

    ranked = sorted(merged.values(), key=lambda f: f.estimated_monthly_savings, reverse=True)
    # Governance findings are informational and legitimately carry no dollar value.
    return [
        finding
        for finding in ranked
        if finding.estimated_monthly_savings >= min_savings or finding.category == "governance"
    ]


def _merge_pair(first: Finding, second: Finding) -> Finding:
    """Combine two views of the same issue, keeping the best part of each."""
    primary, secondary = sorted((first, second), key=lambda f: SOURCE_PRIORITY.index(f.source))

    combined = primary.model_copy(deep=True)
    # Keep whichever remediation actually tells the user what to run.
    if not _has_commands(combined.remediation) and _has_commands(secondary.remediation):
        combined.remediation = secondary.remediation

    seen = {(e.label, e.value) for e in combined.evidence}
    for evidence in secondary.evidence:
        if (evidence.label, evidence.value) not in seen:
            combined.evidence.append(evidence)
            seen.add((evidence.label, evidence.value))

    if primary.source != secondary.source:
        combined.evidence.append(
            Evidence(label="Corroborated by", value=_source_label(secondary.source))
        )
        # Independent agreement from two sources raises confidence.
        combined.confidence = "high"

    if not combined.detail and secondary.detail:
        combined.detail = secondary.detail
    combined.tags = {**secondary.tags, **combined.tags}
    return combined


def _has_commands(remediation: Remediation | None) -> bool:
    return bool(remediation and (remediation.cli or remediation.terraform))


def _source_label(source: str) -> str:
    return {
        "rules": "this agent's own analysis",
        "compute-optimizer": "AWS Compute Optimizer",
        "cost-optimization-hub": "AWS Cost Optimization Hub",
        "trusted-advisor": "AWS Trusted Advisor",
        "cost-explorer": "AWS Cost Explorer",
    }.get(source, source)
