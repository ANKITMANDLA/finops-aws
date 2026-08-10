"""Core data model shared by collectors, rules, the advisor, and the API.

Everything the agent produces is a plain pydantic model so it round-trips cleanly
through SQLite and out to the dashboard as JSON.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Confidence = Literal["high", "medium", "low"]
Effort = Literal["low", "medium", "high"]
Risk = Literal["low", "medium", "high"]

FindingCategory = Literal[
    "idle",
    "rightsizing",
    "storage",
    "network",
    "containers",
    "database",
    "commitments",
    "governance",
]

FindingSource = Literal[
    "rules",
    "compute-optimizer",
    "cost-optimization-hub",
    "trusted-advisor",
    "cost-explorer",
]

# How a dollar figure was derived. Surfaced in the UI so a precise-looking number is
# never mistaken for a billed amount when it is really a list-price estimate.
CostBasis = Literal[
    "actual_resource_level",  # ce:GetCostAndUsageWithResources, real billed cost
    "actual_service_level",  # ce:GetCostAndUsage, allocated down from service totals
    "list_price_estimate",  # computed from the Pricing API
    "aws_recommendation",  # savings figure supplied by an AWS optimization service
    "heuristic",  # rule-of-thumb, lowest confidence
]

COST_BASIS_LABELS: dict[str, str] = {
    "actual_resource_level": "Billed cost (resource level)",
    "actual_service_level": "Billed cost (allocated from service total)",
    "list_price_estimate": "Estimated from list price",
    "aws_recommendation": "AWS recommendation estimate",
    "heuristic": "Heuristic estimate",
}

CapabilityStatus = Literal["ok", "denied", "not_enrolled", "unavailable", "partial", "error"]

HOURS_PER_MONTH = 730.0


def utcnow() -> datetime:
    return datetime.now(UTC)


# Canonical actions. Our rules and the AWS optimization services both normalize onto
# these so that "rightsize this instance" reported by two sources is one finding.
ACTION_TERMINATE = "terminate"
ACTION_STOP = "stop"
ACTION_DELETE = "delete"
ACTION_RIGHTSIZE = "rightsize"
ACTION_MODIFY_STORAGE = "modify_storage"
ACTION_UPGRADE = "upgrade"
ACTION_MIGRATE = "migrate"
ACTION_RELEASE = "release"
ACTION_SET_RETENTION = "set_retention"
ACTION_PURCHASE_COMMITMENT = "purchase_commitment"
ACTION_TAG = "tag"
ACTION_REARCHITECT = "rearchitect"


def make_finding_id(action_type: str, resource_key: str) -> str:
    """Stable identity for a finding, and the de-duplication key.

    Deliberately excludes the rule that produced it: when our own rule and Compute
    Optimizer both say "rightsize i-abc", they must collapse into one finding rather
    than double-counting the same savings. For account-level findings with no resource,
    pass a synthetic key such as ``"account:compute-savings-plan"``.
    """
    raw = f"{action_type}|{resource_key}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


class Evidence(BaseModel):
    """One observed fact that justifies a finding."""

    label: str
    value: str


class Remediation(BaseModel):
    summary: str
    cli: str | None = None
    terraform: str | None = None
    console_path: str | None = None


class Resource(BaseModel):
    """A single AWS resource discovered during inventory."""

    arn: str
    resource_id: str
    resource_type: str  # e.g. "ec2:instance", "ebs:volume", "elbv2:loadbalancer"
    service: str  # coarse grouping used in the UI, e.g. "EC2", "EBS", "EKS"
    region: str
    account_id: str
    name: str | None = None
    availability_zone: str | None = None
    state: str | None = None
    created_at: datetime | None = None
    tags: dict[str, str] = Field(default_factory=dict)
    attributes: dict[str, Any] = Field(default_factory=dict)
    # Utilization summary filled in by the metrics layer, e.g. {"cpu_avg": 2.1}.
    metrics: dict[str, float] = Field(default_factory=dict)
    monthly_cost: float | None = None
    cost_basis: CostBasis | None = None

    @property
    def display_name(self) -> str:
        return self.name or self.tags.get("Name") or self.resource_id


class CostRecord(BaseModel):
    """One cost data point from Cost Explorer, keyed by its grouping dimensions."""

    period_start: date
    period_end: date
    granularity: Literal["HOURLY", "DAILY", "MONTHLY"]
    metric: str = "AmortizedCost"
    amount: float
    unit: str = "USD"
    dimensions: dict[str, str] = Field(default_factory=dict)


class Finding(BaseModel):
    """An actionable cost reduction opportunity with its supporting evidence."""

    id: str
    rule_id: str
    title: str
    category: FindingCategory
    action_type: str  # terminate | rightsize | delete | modify | purchase | tag | rearchitect
    service: str
    source: FindingSource = "rules"
    resource_arn: str | None = None
    resource_id: str | None = None
    resource_type: str | None = None
    region: str | None = None
    estimated_monthly_savings: float = 0.0
    currency: str = "USD"
    confidence: Confidence = "medium"
    implementation_effort: Effort = "low"
    risk: Risk = "low"
    cost_basis: CostBasis = "heuristic"
    rollback_possible: bool = True
    detail: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    remediation: Remediation | None = None
    tags: dict[str, str] = Field(default_factory=dict)

    @property
    def annual_savings(self) -> float:
        return self.estimated_monthly_savings * 12

    @property
    def priority_score(self) -> float:
        """Savings discounted by how hard and risky the change is.

        Ranking purely by dollars puts a Graviton migration above a gp2-to-gp3 switch
        that could be done this afternoon. This weighting surfaces the work worth doing
        first rather than the work with the biggest headline number.
        """
        effort_weight = {"low": 1.0, "medium": 0.7, "high": 0.4}[self.implementation_effort]
        risk_weight = {"low": 1.0, "medium": 0.85, "high": 0.6}[self.risk]
        confidence_weight = {"high": 1.0, "medium": 0.85, "low": 0.6}[self.confidence]
        return round(
            self.estimated_monthly_savings * effort_weight * risk_weight * confidence_weight, 2
        )


class CapabilityNote(BaseModel):
    """Records a data source the scan could not use, so the UI can explain gaps."""

    capability: str
    status: CapabilityStatus
    message: str
    region: str | None = None
    remedy: str | None = None


class BreakdownItem(BaseModel):
    key: str
    amount: float
    share: float = 0.0
    # Optional secondary figure, e.g. savings identified within this slice.
    savings: float = 0.0


class TcoReport(BaseModel):
    """Total cost of ownership for the analysis window, plus the optimized target."""

    period_start: date
    period_end: date
    metric: str = "AmortizedCost"
    currency: str = "USD"

    total_cost: float = 0.0
    daily_run_rate: float = 0.0
    monthly_run_rate: float = 0.0
    month_to_date_cost: float = 0.0
    forecast_next_month: float | None = None
    forecast_lower: float | None = None
    forecast_upper: float | None = None
    previous_period_cost: float | None = None
    change_percent: float | None = None

    identified_monthly_savings: float = 0.0
    optimized_monthly_run_rate: float = 0.0
    savings_percent: float = 0.0

    by_service: list[BreakdownItem] = Field(default_factory=list)
    by_region: list[BreakdownItem] = Field(default_factory=list)
    by_usage_type: list[BreakdownItem] = Field(default_factory=list)
    by_category: list[BreakdownItem] = Field(default_factory=list)
    by_effort: list[BreakdownItem] = Field(default_factory=list)
    daily_trend: list[BreakdownItem] = Field(default_factory=list)

    untagged_monthly_cost: float = 0.0
    commitment_coverage_percent: float | None = None


class ArchitectureRecommendation(BaseModel):
    """A structural change proposed by the LLM advisor, grounded in findings."""

    title: str
    summary: str
    rationale: str
    affected_services: list[str] = Field(default_factory=list)
    estimated_monthly_savings: float | None = None
    implementation_effort: Effort = "medium"
    risk: Risk = "medium"
    steps: list[str] = Field(default_factory=list)
    related_finding_ids: list[str] = Field(default_factory=list)
    tradeoffs: str | None = None


class Advice(BaseModel):
    """Output of the LLM layer: narrative on top of deterministic findings."""

    generated_at: datetime = Field(default_factory=utcnow)
    provider: str = "none"
    model: str = ""
    executive_summary: str = ""
    recommendations: list[ArchitectureRecommendation] = Field(default_factory=list)
    quick_wins: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    error: str | None = None


class ScanMeta(BaseModel):
    """Lightweight scan header used for listings and trend charts."""

    scan_id: str
    account_id: str
    account_alias: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
    duration_seconds: float = 0.0
    regions: list[str] = Field(default_factory=list)
    resource_count: int = 0
    finding_count: int = 0
    monthly_run_rate: float = 0.0
    identified_monthly_savings: float = 0.0


class Scan(BaseModel):
    """The complete result of one scan."""

    scan_id: str
    account_id: str
    account_alias: str | None = None
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    duration_seconds: float = 0.0
    regions: list[str] = Field(default_factory=list)
    resources: list[Resource] = Field(default_factory=list)
    costs: list[CostRecord] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    tco: TcoReport | None = None
    advice: Advice | None = None
    notes: list[CapabilityNote] = Field(default_factory=list)

    @property
    def meta(self) -> ScanMeta:
        return ScanMeta(
            scan_id=self.scan_id,
            account_id=self.account_id,
            account_alias=self.account_alias,
            started_at=self.started_at,
            finished_at=self.finished_at,
            duration_seconds=self.duration_seconds,
            regions=self.regions,
            resource_count=len(self.resources),
            finding_count=len(self.findings),
            monthly_run_rate=self.tco.monthly_run_rate if self.tco else 0.0,
            identified_monthly_savings=self.tco.identified_monthly_savings if self.tco else 0.0,
        )
