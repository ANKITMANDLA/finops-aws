"""Findings about cost visibility rather than cost itself.

These carry no savings figure on purpose. Untagged resources do not waste money by
existing; they waste money by making it impossible to tell whose money it is, which is
what blocks every other decision on this list. They survive the savings filter so the
gap stays visible.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from finops.model import (
    ACTION_TAG,
    Evidence,
    Finding,
    Remediation,
    make_finding_id,
)
from finops.rules.base import Rule, RuleContext, register
from finops.util import human_money

# Words that answer "who pays for this", matched as whole words anywhere in a tag key.
# Organizations routinely namespace their tags — ``acme:business:owner``,
# ``finance/cost-center``, ``Owner_Email`` — and a key that names an owner still names one
# whatever it is prefixed with.
OWNERSHIP_TAG_KEYS = ("owner", "team", "cost-center", "costcenter", "project", "application", "app")

# AWS-managed tags are not evidence that a human labelled anything.
_IGNORED_TAG_PREFIXES = ("aws:", "kubernetes.io/", "eks:", "elasticbeanstalk:")

_WORD_BOUNDARY = re.compile(r"[^a-z0-9]+")


def has_ownership_tag(tags: dict[str, str]) -> bool:
    for key, value in tags.items():
        lowered = key.lower()
        if any(lowered.startswith(prefix) for prefix in _IGNORED_TAG_PREFIXES):
            continue
        if not value.strip():
            continue
        # "company:business:cost-center" becomes "-company-business-cost-center-", so an
        # ownership word only matches on word boundaries and never inside a longer one.
        padded = f"-{_WORD_BOUNDARY.sub('-', lowered).strip('-')}-"
        if any(f"-{word}-" in padded for word in OWNERSHIP_TAG_KEYS):
            return True
    return False


@register
class UnallocatableSpend(Rule):
    """Resources with no ownership tag, and therefore no one to bill."""

    id = "governance.untagged_resources"
    category = "governance"
    title = "Spend that cannot be allocated to an owner"

    # Resource types that are always managed by another resource and are not worth
    # tagging individually.
    IGNORED_TYPES = frozenset({"eks:nodegroup", "eks:fargate-profile", "logs:log-group"})

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        untagged = [
            resource
            for resource in ctx.resources
            if resource.resource_type not in self.IGNORED_TYPES
            and not has_ownership_tag(resource.tags)
        ]
        if not untagged:
            return

        unallocated_cost = sum(ctx.monthly_cost(resource) for resource in untagged)
        by_service: dict[str, int] = {}
        for resource in untagged:
            by_service[resource.service] = by_service.get(resource.service, 0) + 1
        worst = sorted(by_service.items(), key=lambda kv: kv[1], reverse=True)[:6]
        share = len(untagged) / max(len(ctx.resources), 1) * 100

        yield Finding(
            id=make_finding_id(ACTION_TAG, "account:untagged"),
            rule_id=self.id,
            title=(
                f"{len(untagged)} resources have no owner tag, covering "
                f"{human_money(unallocated_cost)}/month"
            ),
            category="governance",
            action_type=ACTION_TAG,
            service="Governance",
            source="rules",
            # Tagging saves nothing directly; it is what makes the rest actionable.
            estimated_monthly_savings=0.0,
            confidence="high",
            implementation_effort="medium",
            risk="low",
            cost_basis="actual_service_level",
            detail=(
                f"{len(untagged)} of {len(ctx.resources)} resources ({share:.0f}%) carry none of "
                f"the ownership tags this agent looks for ({', '.join(OWNERSHIP_TAG_KEYS[:4])}). "
                f"That accounts for about {human_money(unallocated_cost)} a month that cannot be "
                "attributed to a team. No dollar saving is claimed here: the value is that every "
                "other recommendation becomes something you can route to an owner instead of "
                "researching from scratch."
            ),
            evidence=[
                Evidence(
                    label="Untagged resources", value=f"{len(untagged)} of {len(ctx.resources)}"
                ),
                Evidence(
                    label="Unallocatable cost", value=f"{human_money(unallocated_cost)}/month"
                ),
                *[
                    Evidence(label=f"Untagged in {service}", value=str(count))
                    for service, count in worst
                ],
            ],
            remediation=Remediation(
                summary=(
                    "Agree a minimal tag set, activate those keys as cost allocation tags in "
                    "Billing, then enforce them with a tag policy so the gap does not reopen."
                ),
                cli=(
                    "aws resourcegroupstaggingapi tag-resources "
                    "--resource-arn-list <arn> --tags Owner=team-name"
                ),
                console_path="Billing and Cost Management > Cost allocation tags",
            ),
        )


@register
class UntaggedExpensiveResource(Rule):
    """Individually expensive resources with no owner, called out one by one."""

    id = "governance.untagged_expensive_resource"
    category = "governance"
    title = "Expensive resource with no owner"

    # Only worth naming individually above this monthly cost.
    MIN_MONTHLY_COST = 100.0
    MAX_REPORTED = 25

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        candidates = [
            resource
            for resource in ctx.resources
            if ctx.monthly_cost(resource) >= self.MIN_MONTHLY_COST
            and not has_ownership_tag(resource.tags)
        ]
        candidates.sort(key=ctx.monthly_cost, reverse=True)

        for resource in candidates[: self.MAX_REPORTED]:
            cost = ctx.monthly_cost(resource)
            yield Finding(
                id=make_finding_id(ACTION_TAG, resource.arn),
                rule_id=self.id,
                title=(
                    f"{resource.resource_type} {resource.display_name} costs "
                    f"{human_money(cost)}/month with no owner tag"
                ),
                category="governance",
                action_type=ACTION_TAG,
                service=resource.service,
                source="rules",
                resource_arn=resource.arn,
                resource_id=resource.resource_id,
                resource_type=resource.resource_type,
                region=resource.region,
                estimated_monthly_savings=0.0,
                confidence="high",
                implementation_effort="low",
                risk="low",
                cost_basis=resource.cost_basis or "heuristic",
                detail=(
                    "This is one of the most expensive resources in the account and nothing "
                    "records who owns it. Before it can be resized or switched off, someone has "
                    "to work out what it does."
                ),
                evidence=[
                    Evidence(label="Monthly cost", value=f"{human_money(cost)}"),
                    Evidence(label="Region", value=resource.region),
                    Evidence(
                        label="Existing tags",
                        value=", ".join(resource.tags) if resource.tags else "none",
                    ),
                ],
                remediation=Remediation(
                    summary="Add an owner or team tag so the cost can be routed to someone.",
                    cli=(
                        f"aws resourcegroupstaggingapi tag-resources --resource-arn-list "
                        f"{resource.arn} --tags Owner=<team>"
                    ),
                ),
                tags=dict(resource.tags),
            )
