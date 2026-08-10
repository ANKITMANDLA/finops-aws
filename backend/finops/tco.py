"""Assemble the total cost of ownership report.

The headline total always comes from Cost Explorer, never from summing per-resource
estimates: attribution is best-effort, but the bill is not. Savings are then subtracted
from that authoritative run rate to give the optimized target.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from datetime import date

from finops.aws.costs import CostSnapshot
from finops.model import BreakdownItem, Finding, Resource, TcoReport
from finops.rules.governance import has_ownership_tag
from finops.util import percent, round_money, safe_div

logger = logging.getLogger(__name__)

# Longer breakdowns become unreadable in the UI; the tail is grouped instead of dropped.
MAX_BREAKDOWN_ITEMS = 12
MAX_USAGE_TYPES = 15

CATEGORY_LABELS = {
    "idle": "Idle resources",
    "rightsizing": "Rightsizing",
    "storage": "Storage optimization",
    "network": "Networking",
    "containers": "Containers",
    "database": "Databases",
    "commitments": "Commitments",
    "governance": "Governance",
}

EFFORT_LABELS = {"low": "Low effort", "medium": "Medium effort", "high": "High effort"}

# Cost Explorer reports "Amazon Elastic Compute Cloud - Compute" where collectors say
# "EC2", so savings can only be shown beside a cost row once the two vocabularies are
# reconciled. Hints are tried in order against the service names the bill actually
# contains, which keeps this working as AWS renames things.
CE_SERVICE_HINTS: dict[str, tuple[str, ...]] = {
    "EC2": ("Elastic Compute Cloud",),
    "EBS": ("Elastic Block Store", "EC2 - Other", "Elastic Compute Cloud"),
    "EBS Snapshots": ("Elastic Block Store", "EC2 - Other", "Elastic Compute Cloud"),
    "ELB": ("Elastic Load Balancing", "EC2 - Other"),
    "VPC": ("Virtual Private Cloud", "EC2 - Other"),
    "EKS": ("Kubernetes", "Container Service for Kubernetes"),
    "RDS": ("Relational Database Service",),
    "S3": ("Simple Storage Service",),
    "Lambda": ("Lambda",),
    "DynamoDB": ("DynamoDB",),
    "CloudWatch Logs": ("CloudWatch",),
    "Reserved Instances": ("Elastic Compute Cloud",),
    "Savings Plans": ("Elastic Compute Cloud",),
}


def _match_cost_key(service: str, cost_keys: Sequence[str]) -> str | None:
    """Map a finding's service onto the bill's name for it, if there is one."""
    if service in cost_keys:
        return service
    for hint in CE_SERVICE_HINTS.get(service, (service,)):
        for key in cost_keys:
            if hint.lower() in key.lower():
                return key
    return None


def build_tco_report(
    snapshot: CostSnapshot,
    findings: Sequence[Finding],
    resources: Sequence[Resource],
) -> TcoReport:
    """Combine billed cost, identified savings, and inventory into one report."""
    total = snapshot.total_cost
    monthly_run_rate = snapshot.monthly_run_rate
    identified = sum(f.estimated_monthly_savings for f in findings)
    # Savings cannot exceed the bill, however enthusiastic the rules are.
    identified = min(identified, monthly_run_rate) if monthly_run_rate > 0 else identified

    cost_keys = list(snapshot.service_totals)
    savings_by_service = _sum_savings(
        findings, lambda f: _match_cost_key(f.service, cost_keys) or f.service
    )

    report = TcoReport(
        period_start=snapshot.period_start,
        period_end=snapshot.period_end,
        metric=snapshot.metric,
        total_cost=round_money(total),
        daily_run_rate=round_money(snapshot.daily_run_rate),
        monthly_run_rate=round_money(monthly_run_rate),
        month_to_date_cost=round_money(snapshot.month_to_date_cost),
        forecast_next_month=_round_optional(snapshot.forecast_next_month),
        forecast_lower=_round_optional(snapshot.forecast_lower),
        forecast_upper=_round_optional(snapshot.forecast_upper),
        previous_period_cost=_round_optional(snapshot.previous_period_cost),
        change_percent=_change_percent(total, snapshot.previous_period_cost),
        identified_monthly_savings=round_money(identified),
        optimized_monthly_run_rate=round_money(max(monthly_run_rate - identified, 0.0)),
        savings_percent=percent(identified, monthly_run_rate),
        by_service=_breakdown(snapshot.service_totals, total, savings=savings_by_service),
        by_region=_breakdown(snapshot.region_totals, total),
        by_usage_type=_breakdown(snapshot.usage_type_totals, total, limit=MAX_USAGE_TYPES),
        by_category=_savings_breakdown(findings, lambda f: f.category, CATEGORY_LABELS),
        by_effort=_savings_breakdown(findings, lambda f: f.implementation_effort, EFFORT_LABELS),
        daily_trend=_daily_trend(snapshot.daily_totals),
        untagged_monthly_cost=round_money(_untagged_cost(resources)),
        commitment_coverage_percent=snapshot.commitments.blended_coverage_percent,
    )
    logger.info(
        "TCO: %.2f run rate/month, %.2f identified savings (%.1f%%)",
        report.monthly_run_rate,
        report.identified_monthly_savings,
        report.savings_percent,
    )
    return report


def rank_findings(findings: Iterable[Finding]) -> list[Finding]:
    """Order findings by value against effort, not by raw dollars."""
    return sorted(
        findings,
        key=lambda f: (f.priority_score, f.estimated_monthly_savings),
        reverse=True,
    )


def _round_optional(value: float | None) -> float | None:
    return round_money(value) if value is not None else None


def _change_percent(current: float, previous: float | None) -> float | None:
    if previous is None or previous <= 0:
        return None
    return round((current - previous) / previous * 100.0, 2)


def _breakdown(
    totals: dict[str, float],
    grand_total: float,
    *,
    limit: int = MAX_BREAKDOWN_ITEMS,
    savings: dict[str, float] | None = None,
) -> list[BreakdownItem]:
    """Rank a cost dimension, folding the long tail into a single "Other" row."""
    if not totals:
        return []
    ordered = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    head, tail = ordered[:limit], ordered[limit:]

    items = [
        BreakdownItem(
            key=key,
            amount=round_money(amount),
            share=percent(amount, grand_total),
            savings=round_money((savings or {}).get(key, 0.0)),
        )
        for key, amount in head
    ]
    if tail:
        tail_total = sum(amount for _, amount in tail)
        items.append(
            BreakdownItem(
                key=f"Other ({len(tail)} more)",
                amount=round_money(tail_total),
                share=percent(tail_total, grand_total),
                savings=round_money(sum((savings or {}).get(key, 0.0) for key, _ in tail)),
            )
        )
    return items


def _sum_savings(findings: Sequence[Finding], key) -> dict[str, float]:
    totals: dict[str, float] = {}
    for finding in findings:
        bucket = key(finding)
        totals[bucket] = totals.get(bucket, 0.0) + finding.estimated_monthly_savings
    return totals


def _savings_breakdown(
    findings: Sequence[Finding], key, labels: dict[str, str]
) -> list[BreakdownItem]:
    totals = _sum_savings(findings, key)
    grand_total = sum(totals.values())
    return [
        BreakdownItem(
            key=labels.get(bucket, bucket),
            amount=round_money(amount),
            share=percent(amount, grand_total),
            savings=round_money(amount),
        )
        for bucket, amount in sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
        if amount > 0
    ]


def _daily_trend(daily_totals: dict[str, float]) -> list[BreakdownItem]:
    return [
        BreakdownItem(key=day, amount=round_money(amount))
        for day, amount in sorted(daily_totals.items())
    ]


def _untagged_cost(resources: Sequence[Resource]) -> float:
    return sum(
        resource.monthly_cost or 0.0
        for resource in resources
        if not has_ownership_tag(resource.tags)
    )


def summarize_for_advisor(
    report: TcoReport,
    findings: Sequence[Finding],
    resources: Sequence[Resource],
    *,
    max_findings: int = 40,
) -> dict:
    """Compact, token-cheap summary of a scan for the LLM advisor.

    The model never sees raw inventory. It gets aggregates plus the top findings, which
    keeps the prompt small and stops it from inventing per-resource detail it cannot
    verify.
    """
    inventory: dict[str, dict[str, float]] = {}
    for resource in resources:
        entry = inventory.setdefault(resource.resource_type, {"count": 0, "monthly_cost": 0.0})
        entry["count"] += 1
        entry["monthly_cost"] += resource.monthly_cost or 0.0

    regions: dict[str, int] = {}
    for resource in resources:
        regions[resource.region] = regions.get(resource.region, 0) + 1

    return {
        "period": {
            "start": report.period_start.isoformat(),
            "end": report.period_end.isoformat(),
        },
        "monthly_run_rate": report.monthly_run_rate,
        "optimized_monthly_run_rate": report.optimized_monthly_run_rate,
        "identified_monthly_savings": report.identified_monthly_savings,
        "savings_percent": report.savings_percent,
        "forecast_next_month": report.forecast_next_month,
        "commitment_coverage_percent": report.commitment_coverage_percent,
        "untagged_monthly_cost": report.untagged_monthly_cost,
        "cost_by_service": [
            {"service": item.key, "monthly_cost": item.amount, "share_percent": item.share}
            for item in report.by_service[:10]
        ],
        "cost_by_region": [
            {"region": item.key, "monthly_cost": item.amount} for item in report.by_region[:8]
        ],
        "savings_by_category": [
            {"category": item.key, "monthly_savings": item.amount} for item in report.by_category
        ],
        "inventory": [
            {
                "resource_type": resource_type,
                "count": int(entry["count"]),
                "monthly_cost": round_money(entry["monthly_cost"]),
            }
            for resource_type, entry in sorted(
                inventory.items(), key=lambda kv: kv[1]["monthly_cost"], reverse=True
            )[:20]
        ],
        "regions_in_use": sorted(regions, key=regions.get, reverse=True)[:10],
        "top_findings": [
            {
                "id": finding.id,
                "title": finding.title,
                "category": finding.category,
                "service": finding.service,
                "monthly_savings": finding.estimated_monthly_savings,
                "effort": finding.implementation_effort,
                "risk": finding.risk,
                "confidence": finding.confidence,
                "source": finding.source,
            }
            for finding in rank_findings(findings)[:max_findings]
        ],
    }


def compare_scans(current: TcoReport, previous: TcoReport | None) -> dict[str, float | None]:
    """Scan-over-scan deltas for the trends view."""
    if previous is None:
        return {
            "run_rate_change": None,
            "run_rate_change_percent": None,
            "savings_change": None,
        }
    run_rate_change = current.monthly_run_rate - previous.monthly_run_rate
    return {
        "run_rate_change": round_money(run_rate_change),
        "run_rate_change_percent": round(
            safe_div(run_rate_change, previous.monthly_run_rate) * 100, 2
        ),
        "savings_change": round_money(
            current.identified_monthly_savings - previous.identified_monthly_savings
        ),
    }


def empty_report() -> TcoReport:
    today = date.today()
    return TcoReport(period_start=today, period_end=today)
