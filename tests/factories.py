"""Helpers for building model objects in tests."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from finops.model import (
    CostRecord,
    Evidence,
    Finding,
    Remediation,
    Resource,
    Scan,
    TcoReport,
    make_finding_id,
    utcnow,
)


def make_resource(
    resource_id: str = "i-0123456789abcdef0",
    *,
    resource_type: str = "ec2:instance",
    service: str = "EC2",
    region: str = "us-east-1",
    state: str | None = "running",
    tags: dict[str, str] | None = None,
    attributes: dict[str, Any] | None = None,
    metrics: dict[str, float] | None = None,
    monthly_cost: float | None = 100.0,
    created_days_ago: int | None = 60,
    availability_zone: str | None = "us-east-1a",
) -> Resource:
    created_at = (
        datetime.now(UTC) - timedelta(days=created_days_ago)
        if created_days_ago is not None
        else None
    )
    return Resource(
        arn=f"arn:aws:ec2:{region}:111122223333:{resource_type.split(':')[-1]}/{resource_id}",
        resource_id=resource_id,
        resource_type=resource_type,
        service=service,
        region=region,
        account_id="111122223333",
        name=(tags or {}).get("Name"),
        availability_zone=availability_zone,
        state=state,
        created_at=created_at,
        tags=tags or {},
        attributes=attributes or {},
        metrics=metrics or {},
        monthly_cost=monthly_cost,
        cost_basis="list_price_estimate" if monthly_cost is not None else None,
    )


def make_finding(
    rule_id: str = "ec2.idle_instance",
    *,
    savings: float = 42.5,
    category: str = "idle",
    service: str = "EC2",
    resource_arn: str | None = "arn:aws:ec2:us-east-1:111122223333:instance/i-0123456789abcdef0",
    source: str = "rules",
    action: str = "terminate",
) -> Finding:
    return Finding(
        id=make_finding_id(action, resource_arn or rule_id),
        rule_id=rule_id,
        title="Idle EC2 instance",
        category=category,  # type: ignore[arg-type]
        action_type=action,
        service=service,
        source=source,  # type: ignore[arg-type]
        resource_arn=resource_arn,
        resource_id="i-0123456789abcdef0",
        resource_type="ec2:instance",
        region="us-east-1",
        estimated_monthly_savings=savings,
        confidence="high",
        implementation_effort="low",
        risk="medium",
        cost_basis="list_price_estimate",
        rollback_possible=False,
        detail="Average CPU below 5% for 14 days.",
        evidence=[Evidence(label="Average CPU", value="1.2%")],
        remediation=Remediation(
            summary="Terminate the instance after confirming ownership.",
            cli="aws ec2 terminate-instances --instance-ids i-0123456789abcdef0",
        ),
    )


def make_scan(
    scan_id: str = "scan-test-1",
    *,
    resources: list[Resource] | None = None,
    findings: list[Finding] | None = None,
    costs: list[CostRecord] | None = None,
    tco: TcoReport | None = None,
) -> Scan:
    return Scan(
        scan_id=scan_id,
        account_id="111122223333",
        account_alias="sandbox",
        started_at=utcnow(),
        finished_at=utcnow(),
        duration_seconds=12.5,
        regions=["us-east-1", "us-west-2"],
        resources=resources if resources is not None else [make_resource()],
        findings=findings if findings is not None else [make_finding()],
        costs=(
            costs
            if costs is not None
            else [
                CostRecord(
                    period_start=date(2026, 7, 1),
                    period_end=date(2026, 7, 2),
                    granularity="DAILY",
                    amount=31.4,
                    dimensions={"SERVICE": "Amazon Elastic Compute Cloud - Compute"},
                )
            ]
        ),
        tco=tco
        or TcoReport(
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            total_cost=942.0,
            monthly_run_rate=942.0,
            identified_monthly_savings=42.5,
            optimized_monthly_run_rate=899.5,
        ),
    )
