"""Savings Plans and Reserved Instances.

These findings are account-level rather than resource-level, so they carry a synthetic
identity. The savings figures come straight from Cost Explorer's own recommendation
engine, which sees the billing data we cannot.
"""

from __future__ import annotations

from collections.abc import Iterable

from finops.model import (
    ACTION_PURCHASE_COMMITMENT,
    ACTION_RIGHTSIZE,
    Evidence,
    Finding,
    Remediation,
    make_finding_id,
)
from finops.rules.base import Rule, RuleContext, register
from finops.util import human_money

# Commitments below this monthly saving are not worth locking in a year of spend for.
MIN_COMMITMENT_SAVINGS = 5.0


@register
class SavingsPlanOpportunity(Rule):
    """Cost Explorer's own Compute Savings Plan recommendation."""

    id = "commitments.savings_plan_gap"
    category = "commitments"
    title = "Compute Savings Plan opportunity"

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        recommendation = ctx.cost.commitments.savings_plans_recommendation
        if not recommendation:
            return
        savings = recommendation.get("estimated_monthly_savings", 0.0)
        if savings < MIN_COMMITMENT_SAVINGS:
            return

        hourly = recommendation.get("hourly_commitment", 0.0)
        coverage = ctx.cost.commitments.savings_plans_coverage_percent

        yield Finding(
            id=make_finding_id(ACTION_PURCHASE_COMMITMENT, "account:compute-savings-plan"),
            rule_id=self.id,
            title=(
                f"Buy a Compute Savings Plan at ${hourly:.2f}/hour to save "
                f"{human_money(savings)}/month"
            ),
            category="commitments",
            action_type=ACTION_PURCHASE_COMMITMENT,
            service="Savings Plans",
            source="cost-explorer",
            estimated_monthly_savings=round(savings, 2),
            confidence="high",
            implementation_effort="low",
            risk="medium",
            cost_basis="aws_recommendation",
            rollback_possible=False,
            detail=(
                "Cost Explorer analysed the last 30 days of usage and recommends a one year, "
                "no upfront Compute Savings Plan. Compute Savings Plans apply across EC2, "
                "Fargate, and Lambda in any region and any instance family, so they are the "
                "flexible option. The commitment is billed hourly for the full term whether or "
                "not you use it, so size it to your steady-state floor, not your peak."
            ),
            evidence=[
                Evidence(label="Recommended commitment", value=f"${hourly:.2f}/hour"),
                Evidence(label="Estimated monthly savings", value=human_money(savings)),
                Evidence(
                    label="Estimated savings rate",
                    value=f"{recommendation.get('estimated_savings_percentage', 0):.1f}%",
                ),
                Evidence(
                    label="Current on-demand spend",
                    value=human_money(recommendation.get("current_on_demand_spend", 0.0)),
                ),
                Evidence(
                    label="Current coverage",
                    value=f"{coverage:.1f}%" if coverage is not None else "none",
                ),
                Evidence(label="Term", value=str(recommendation.get("term"))),
            ],
            remediation=Remediation(
                summary=(
                    "Review the recommendation in Cost Explorer and purchase. Start with a "
                    "smaller commitment than suggested if your usage is still changing."
                ),
                console_path="Billing and Cost Management > Savings Plans > Recommendations",
            ),
        )


@register
class ReservedInstanceOpportunity(Rule):
    """Per-service Reserved Instance recommendations from Cost Explorer."""

    id = "commitments.reservation_gap"
    category = "commitments"
    title = "Reserved Instance opportunity"

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        for recommendation in ctx.cost.commitments.reservation_recommendations:
            savings = recommendation.get("estimated_monthly_savings", 0.0)
            if savings < MIN_COMMITMENT_SAVINGS:
                continue
            service = recommendation.get("service", "AWS")

            yield Finding(
                id=make_finding_id(ACTION_PURCHASE_COMMITMENT, f"account:ri:{service}"),
                rule_id=self.id,
                title=f"Reserved Instances for {service} would save {human_money(savings)}/month",
                category="commitments",
                action_type=ACTION_PURCHASE_COMMITMENT,
                service="Reserved Instances",
                source="cost-explorer",
                estimated_monthly_savings=round(savings, 2),
                currency=recommendation.get("currency", "USD"),
                confidence="high",
                implementation_effort="low",
                risk="medium",
                cost_basis="aws_recommendation",
                rollback_possible=False,
                detail=(
                    f"Cost Explorer recommends Reserved Instances for {service} based on the "
                    "last 30 days. Reservations are tied to a specific instance family and "
                    "region, so they save more than a Savings Plan but bind you to a shape. "
                    "Prefer a Savings Plan if the workload might be resized or moved."
                ),
                evidence=[
                    Evidence(label="Service", value=service),
                    Evidence(label="Estimated monthly savings", value=human_money(savings)),
                    Evidence(label="Term", value=str(recommendation.get("term"))),
                ],
                remediation=Remediation(
                    summary="Review and purchase from the Reserved Instance recommendations page.",
                    console_path="Billing and Cost Management > Reservations > Recommendations",
                ),
            )


@register
class UnderusedCommitment(Rule):
    """A commitment you are not consuming is money already spent for nothing."""

    id = "commitments.low_utilization"
    category = "commitments"
    title = "Under-used commitment"

    # Commitments are meant to sit near 100%; below this, capacity is being wasted.
    UTILIZATION_TARGET = 95.0

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        commitments = ctx.cost.commitments
        for label, utilization, key in (
            ("Savings Plans", commitments.savings_plans_utilization_percent, "sp"),
            ("Reserved Instances", commitments.reservation_utilization_percent, "ri"),
        ):
            if utilization is None or utilization >= self.UTILIZATION_TARGET:
                continue

            # The unused share of committed spend is the waste. Without the commitment's
            # absolute value we can only express it as a proportion of covered spend.
            wasted_fraction = (100.0 - utilization) / 100.0
            covered_spend = ctx.cost.monthly_run_rate * (
                (commitments.blended_coverage_percent or 0.0) / 100.0
            )
            savings = covered_spend * wasted_fraction

            yield Finding(
                id=make_finding_id(ACTION_RIGHTSIZE, f"account:commitment-utilization:{key}"),
                rule_id=self.id,
                title=f"{label} are only {utilization:.1f}% used",
                category="commitments",
                action_type=ACTION_RIGHTSIZE,
                service=label,
                source="cost-explorer",
                estimated_monthly_savings=round(max(savings, 0.0), 2),
                confidence="medium",
                implementation_effort="medium",
                risk="low",
                cost_basis="actual_service_level",
                detail=(
                    f"{label} utilization is {utilization:.1f}%, so roughly "
                    f"{wasted_fraction:.0%} of what you have already committed to is going "
                    "unused. This usually means the workload the commitment was bought for was "
                    "resized, moved region, or shut down. You cannot cancel the commitment, but "
                    "you can shift workloads back onto the covered shape, or sell unused "
                    "Standard RIs on the Reserved Instance Marketplace."
                ),
                evidence=[
                    Evidence(label="Utilization", value=f"{utilization:.1f}%"),
                    Evidence(label="Target", value=f"{self.UTILIZATION_TARGET:.0f}%"),
                    Evidence(
                        label="Coverage",
                        value=f"{commitments.blended_coverage_percent:.1f}%"
                        if commitments.blended_coverage_percent is not None
                        else "unknown",
                    ),
                ],
                remediation=Remediation(
                    summary=(
                        "Find what changed since the commitment was purchased and move "
                        "workloads back onto the covered instance family or region."
                    ),
                    console_path="Billing and Cost Management > Savings Plans > Utilization",
                ),
            )


@register
class LowCommitmentCoverage(Rule):
    """Steady-state usage running entirely at on-demand rates."""

    id = "commitments.low_coverage"
    category = "commitments"
    title = "Low commitment coverage on steady spend"

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        commitments = ctx.cost.commitments
        coverage = commitments.blended_coverage_percent
        if coverage is None or coverage >= ctx.thresholds.commitment_coverage_target_percent:
            return
        # If Cost Explorer already produced a purchase recommendation, that finding is
        # more specific and carries a real number; this one would just repeat it.
        if commitments.savings_plans_recommendation:
            return

        monthly = ctx.cost.monthly_run_rate
        if monthly <= 0:
            return
        target = ctx.thresholds.commitment_coverage_target_percent
        uncovered_share = (target - coverage) / 100.0
        # A one year no upfront Compute Savings Plan discounts around 20%.
        savings = monthly * uncovered_share * 0.20

        yield Finding(
            id=make_finding_id(ACTION_PURCHASE_COMMITMENT, "account:coverage-gap"),
            rule_id=self.id,
            title=f"Only {coverage:.0f}% of compute spend is covered by a commitment",
            category="commitments",
            action_type=ACTION_PURCHASE_COMMITMENT,
            service="Savings Plans",
            source="rules",
            estimated_monthly_savings=round(max(savings, 0.0), 2),
            confidence="low",
            implementation_effort="low",
            risk="medium",
            cost_basis="heuristic",
            rollback_possible=False,
            detail=(
                f"Commitment coverage is {coverage:.0f}% against a target of {target:.0f}%. "
                "Any workload that runs continuously is paying the full on-demand rate. The "
                "figure here is a rule-of-thumb based on a 20% Savings Plan discount applied to "
                "the coverage gap; Cost Explorer's own recommendation, once it has enough "
                "history, will be more precise."
            ),
            evidence=[
                Evidence(label="Current coverage", value=f"{coverage:.1f}%"),
                Evidence(label="Target coverage", value=f"{target:.0f}%"),
                Evidence(label="Monthly run rate", value=human_money(monthly)),
                Evidence(label="Estimate basis", value="20% discount on the uncovered share"),
            ],
            remediation=Remediation(
                summary=(
                    "Identify the always-on portion of your compute and cover it with a Compute "
                    "Savings Plan."
                ),
                console_path="Billing and Cost Management > Savings Plans > Recommendations",
            ),
        )
