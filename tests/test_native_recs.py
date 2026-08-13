from __future__ import annotations

from typing import Any

import pytest
from tests.factories import make_finding
from tests.fakes import FakeAwsContext, client_error

from finops.aws.errors import NoteCollector
from finops.aws.native_recs import (
    NativeRecommendations,
    _extract_savings,
    _humanize,
    _trusted_advisor_action,
)
from finops.model import (
    ACTION_MODIFY_STORAGE,
    ACTION_PURCHASE_COMMITMENT,
    ACTION_RELEASE,
    ACTION_RIGHTSIZE,
    make_finding_id,
)
from finops.rules import merge_findings

INSTANCE_ARN = "arn:aws:ec2:us-east-1:111122223333:instance/i-0abc"


def savings(value: float, percentage: float = 30.0) -> dict[str, Any]:
    return {
        "savingsOpportunityPercentage": percentage,
        "estimatedMonthlySavings": {"currency": "USD", "value": value},
    }


class FakeOptimizerClient:
    def __init__(self, *, status: str = "Active", empty: bool = False) -> None:
        self.status = status
        self.empty = empty

    def get_enrollment_status(self) -> dict[str, Any]:
        return {"status": self.status}

    def get_ec2_instance_recommendations(self, **kwargs: Any) -> dict[str, Any]:
        if self.empty:
            return {"instanceRecommendations": []}
        return {
            "instanceRecommendations": [
                {
                    "instanceArn": INSTANCE_ARN,
                    "instanceName": "web-1",
                    "currentInstanceType": "m5.4xlarge",
                    "finding": "Overprovisioned",
                    "findingReasonCodes": ["CPUOverprovisioned", "MemoryOverprovisioned"],
                    "tags": [{"key": "env", "value": "prod"}],
                    "recommendationOptions": [
                        {
                            "instanceType": "m5.2xlarge",
                            "rank": 1,
                            "savingsOpportunity": savings(120.0),
                        },
                        {
                            "instanceType": "m6i.2xlarge",
                            "rank": 2,
                            "savingsOpportunity": savings(180.0, 45.0),
                        },
                    ],
                },
                {
                    # Already optimal: must not produce a finding.
                    "instanceArn": "arn:aws:ec2:us-east-1:111122223333:instance/i-good",
                    "currentInstanceType": "t3.micro",
                    "finding": "Optimized",
                    "recommendationOptions": [],
                },
            ]
        }

    def get_ebs_volume_recommendations(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "volumeRecommendations": [
                {
                    "volumeArn": "arn:aws:ec2:us-east-1:111122223333:volume/vol-0abc",
                    "finding": "NotOptimized",
                    "currentConfiguration": {"volumeType": "gp2", "volumeSize": 500},
                    "volumeRecommendationOptions": [
                        {
                            "configuration": {"volumeType": "gp3", "volumeSize": 500},
                            "rank": 1,
                            "savingsOpportunity": savings(10.0, 20.0),
                        }
                    ],
                }
            ]
        }

    def get_lambda_function_recommendations(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "lambdaFunctionRecommendations": [
                {
                    "functionArn": "arn:aws:lambda:us-east-1:111122223333:function:api",
                    "functionName": "api",
                    "currentMemorySize": 3008,
                    "finding": "NotOptimized",
                    "findingReasonCodes": ["MemoryOverprovisioned"],
                    "memorySizeRecommendationOptions": [
                        {"memorySize": 512, "rank": 1, "savingsOpportunity": savings(8.0)}
                    ],
                }
            ]
        }

    def get_auto_scaling_group_recommendations(self, **kwargs: Any) -> dict[str, Any]:
        return {"autoScalingGroupRecommendations": []}

    def get_idle_recommendations(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "idleRecommendations": [
                {
                    "resourceArn": "arn:aws:rds:us-east-1:111122223333:db:legacy",
                    "resourceType": "RDSDBInstance",
                    "finding": "Idle",
                    "findingDescription": "No connections for 14 days",
                    "savingsOpportunity": savings(220.0),
                }
            ]
        }


class FakeHubClient:
    def __init__(self, *, active: bool = True) -> None:
        self.active = active

    def list_enrollment_statuses(self, **kwargs: Any) -> dict[str, Any]:
        return {"items": [{"status": "Active" if self.active else "Inactive"}]}

    def list_recommendations(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "items": [
                {
                    "recommendationId": "r-1",
                    "resourceArn": INSTANCE_ARN,
                    "resourceId": "i-0abc",
                    "region": "us-east-1",
                    "actionType": "Rightsize",
                    "currentResourceType": "Ec2Instance",
                    "currentResourceSummary": "m5.4xlarge",
                    "recommendedResourceSummary": "m5.2xlarge",
                    "estimatedMonthlySavings": 150.0,
                    "estimatedSavingsPercentage": 40.0,
                    "estimatedMonthlyCost": 225.0,
                    "implementationEffort": "Medium",
                    "restartNeeded": True,
                    "rollbackPossible": True,
                },
                {
                    "recommendationId": "r-2",
                    "actionType": "PurchaseSavingsPlans",
                    "currentResourceType": "ComputeSavingsPlans",
                    "recommendedResourceSummary": "1yr No Upfront Compute SP",
                    "estimatedMonthlySavings": 400.0,
                    "implementationEffort": "VeryLow",
                    "rollbackPossible": False,
                },
                {
                    # Zero savings is not actionable.
                    "recommendationId": "r-3",
                    "actionType": "Rightsize",
                    "resourceId": "i-zero",
                    "estimatedMonthlySavings": 0.0,
                },
            ]
        }


class FakeSupportClient:
    def __init__(self, *, denied: bool = False) -> None:
        self.denied = denied

    def describe_trusted_advisor_checks(self, **kwargs: Any) -> dict[str, Any]:
        if self.denied:
            raise client_error("SubscriptionRequiredException", "DescribeTrustedAdvisorChecks")
        return {
            "checks": [
                {
                    "id": "check-1",
                    "name": "Idle Load Balancers",
                    "category": "cost_optimizing",
                    "description": "Load balancers with no traffic.",
                    "metadata": ["Region", "Load Balancer Name", "Estimated Monthly Savings"],
                },
                {"id": "check-2", "name": "IAM Password Policy", "category": "security"},
            ]
        }

    def describe_trusted_advisor_check_result(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "result": {
                "status": "warning",
                "flaggedResources": [
                    {
                        "resourceId": "lb-idle-1",
                        "region": "us-east-1",
                        "metadata": ["us-east-1", "lb-idle-1", "$18.30"],
                    },
                    {"resourceId": "suppressed", "isSuppressed": True, "metadata": []},
                ],
            }
        }


class RoutingClient:
    """Dispatches to the right fake based on which service was requested."""

    def __init__(self, mapping: dict[str, Any]) -> None:
        self.mapping = mapping

    def __call__(self, service: str, region: str | None = None) -> Any:
        return self.mapping[service]


class RoutingAwsContext(FakeAwsContext):
    def __init__(self, mapping: dict[str, Any], settings: Any) -> None:
        super().__init__(None, settings)
        self._mapping = mapping

    def client(self, service: str, region: str | None = None) -> Any:
        if service not in self._mapping:
            raise client_error("AccessDenied", service)
        return self._mapping[service]


@pytest.fixture
def native(settings):
    mapping = {
        "compute-optimizer": FakeOptimizerClient(),
        "cost-optimization-hub": FakeHubClient(),
        "support": FakeSupportClient(),
    }
    notes = NoteCollector()
    return NativeRecommendations(RoutingAwsContext(mapping, settings), notes), mapping, notes


def test_compute_optimizer_picks_the_highest_saving_option(native):
    recommendations, _, _ = native
    findings = recommendations.compute_optimizer("us-east-1")

    resize = next(f for f in findings if f.resource_id == "i-0abc")
    # The second option saves more than the first, despite ranking lower.
    assert resize.estimated_monthly_savings == 180.0
    assert "m6i.2xlarge" in resize.title
    assert resize.action_type == ACTION_RIGHTSIZE
    assert resize.cost_basis == "aws_recommendation"
    assert resize.source == "compute-optimizer"
    assert resize.tags == {"env": "prod"}
    labels = {e.label for e in resize.evidence}
    assert {"Compute Optimizer finding", "Current", "Recommended", "Reason"} <= labels


def test_optimized_resources_produce_no_findings(native):
    recommendations, _, _ = native
    findings = recommendations.compute_optimizer("us-east-1")
    assert not any(f.resource_id == "i-good" for f in findings)


def test_compute_optimizer_covers_volumes_lambda_and_idle(native):
    recommendations, _, _ = native
    findings = recommendations.compute_optimizer("us-east-1")
    by_service = {f.service: f for f in findings}

    assert by_service["EBS"].estimated_monthly_savings == 10.0
    assert by_service["EBS"].category == "storage"
    assert "512 MB" in by_service["Lambda"].title
    assert by_service["RDS"].category == "idle"
    assert by_service["RDS"].estimated_monthly_savings == 220.0
    assert by_service["RDS"].title == "Idle RDSDB Instance: legacy"


def test_unenrolled_compute_optimizer_is_reported_not_failed(settings):
    mapping = {"compute-optimizer": FakeOptimizerClient(status="Inactive")}
    notes = NoteCollector()
    findings = NativeRecommendations(RoutingAwsContext(mapping, settings), notes).compute_optimizer(
        "us-east-1"
    )

    assert findings == []
    note = next(n for n in notes.notes if n.capability == "compute-optimizer")
    assert note.status == "not_enrolled"
    assert "console.aws.amazon.com/compute-optimizer" in (note.remedy or "")


def test_cost_optimization_hub_normalizes_actions_and_effort(native):
    recommendations, _, _ = native
    findings = recommendations.cost_optimization_hub()

    rightsize = next(f for f in findings if f.action_type == ACTION_RIGHTSIZE)
    assert rightsize.estimated_monthly_savings == 150.0
    assert rightsize.implementation_effort == "medium"
    # A restart makes the change riskier than a pure config edit.
    assert rightsize.risk == "high"
    assert rightsize.service == "EC2"

    commitment = next(f for f in findings if f.action_type == ACTION_PURCHASE_COMMITMENT)
    assert commitment.category == "commitments"
    assert commitment.implementation_effort == "low"
    assert commitment.rollback_possible is False
    # Account-level advice still needs a stable, non-colliding id.
    assert commitment.id and commitment.resource_arn is None

    assert not any(f.estimated_monthly_savings == 0 for f in findings)


def test_hub_and_optimizer_agree_on_the_id_for_the_same_action(native):
    recommendations, _, _ = native
    optimizer = next(
        f for f in recommendations.compute_optimizer("us-east-1") if f.resource_id == "i-0abc"
    )
    hub = next(f for f in recommendations.cost_optimization_hub() if f.resource_id == "i-0abc")
    # Same resource, same action: the ids must match so de-duplication can merge them.
    assert optimizer.id == hub.id == make_finding_id(ACTION_RIGHTSIZE, INSTANCE_ARN)


def test_disabled_hub_is_reported_not_failed(settings):
    mapping = {"cost-optimization-hub": FakeHubClient(active=False)}
    notes = NoteCollector()
    findings = NativeRecommendations(
        RoutingAwsContext(mapping, settings), notes
    ).cost_optimization_hub()

    assert findings == []
    assert next(n for n in notes.notes if n.capability == "cost-optimization-hub").status == (
        "not_enrolled"
    )


def test_trusted_advisor_reads_only_cost_checks_and_skips_suppressed(native):
    recommendations, _, _ = native
    findings = recommendations.trusted_advisor()

    assert len(findings) == 1
    finding = findings[0]
    assert finding.resource_id == "lb-idle-1"
    assert finding.estimated_monthly_savings == 18.30
    assert finding.service == "ELB"
    assert finding.source == "trusted-advisor"


class FakeLowUtilizationSupportClient:
    """The check that dominates a real account: Low Utilization Amazon EC2 Instances."""

    def describe_trusted_advisor_checks(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "checks": [
                {
                    "id": "Qch7DwouX1",
                    "name": "Low Utilization Amazon EC2 Instances",
                    "category": "cost_optimizing",
                    "metadata": [
                        "Region/AZ",
                        "Instance ID",
                        "Instance Name",
                        "Instance Type",
                        "Estimated Monthly Savings",
                    ],
                }
            ]
        }

    def describe_trusted_advisor_check_result(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "result": {
                "status": "warning",
                "flaggedResources": [
                    {
                        # Trusted Advisor's own id is an opaque per-check hash.
                        "resourceId": "iC9CwOp2-7dZjAv3KpQ",
                        "region": "us-east-1",
                        "metadata": [
                            "us-east-1a",
                            "i-0abc123def456789",
                            "batch-worker",
                            "m5.4xlarge",
                            "$2,211.84",
                        ],
                    }
                ],
            }
        }


def test_trusted_advisor_findings_carry_the_real_resource_identity(settings):
    """The opaque check id is useless; the instance id is what you act on."""
    mapping = {"support": FakeLowUtilizationSupportClient()}
    context = RoutingAwsContext(mapping, settings)
    findings = NativeRecommendations(context, NoteCollector()).trusted_advisor()

    finding = findings[0]
    assert finding.resource_id == "i-0abc123def456789"
    assert finding.estimated_monthly_savings == 2211.84
    # An ARN lets the finding join up with the same instance in the inventory, and lets
    # de-duplication collapse it against our own rightsizing rule.
    assert finding.resource_arn == (
        f"arn:aws:ec2:us-east-1:{context.account_id}:instance/i-0abc123def456789"
    )
    assert finding.action_type == ACTION_RIGHTSIZE
    assert finding.category == "rightsizing"


class FakeCostOptimizationHubBackedClient:
    """The newer checks that relay Cost Optimization Hub, whose columns lead with its own id."""

    def describe_trusted_advisor_checks(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "checks": [
                {
                    "id": "c1z7kmr02n",
                    "name": "Amazon EBS cost optimization recommendations for volumes",
                    "category": "cost_optimizing",
                    # The real column order, which puts a percentage before the amount and
                    # five columns of plumbing before anything worth reading.
                    "metadata": [
                        "Status",
                        "Region",
                        "Recommendation Id",
                        "Resource Id",
                        "Resource ARN",
                        "Current Resource Type",
                        "Recommended Action",
                        "Current Resource Summary",
                        "Recommended Resource Summary",
                        "Estimated Savings Percentage",
                        "Estimated Monthly Cost",
                        "Estimated Monthly Savings",
                        "Currency Code",
                    ],
                }
            ]
        }

    def describe_trusted_advisor_check_result(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "result": {
                "status": "warning",
                "flaggedResources": [
                    {
                        "resourceId": "opaque-hash",
                        "metadata": [
                            "Yellow",
                            "us-east-1",
                            "1fc83e286af77b3c",
                            "vol-04dec132e120282f1",
                            "arn:aws:ec2:us-east-1:111122223333:volume/vol-04dec132e120282f1",
                            "EbsVolume",
                            "Rightsize",
                            "150.0 GB Storage/4000.0 IOPS/125.0 MB/s Throughput",
                            "150.0 GB Storage/3000.0 IOPS/125.0 MB/s Throughput",
                            "29.41",
                            "17.0",
                            "5.0",
                            "USD",
                        ],
                    }
                ],
            }
        }


def test_a_recommendation_id_is_never_mistaken_for_the_resource(settings):
    """These checks list their own id first, and it matches nothing in the account.

    A finding that cannot name its resource cannot be merged against our own view of that
    resource, so its savings land in the total on top of ours for the same volume.
    """
    mapping = {"support": FakeCostOptimizationHubBackedClient()}
    findings = NativeRecommendations(
        RoutingAwsContext(mapping, settings), NoteCollector()
    ).trusted_advisor()

    finding = findings[0]
    assert finding.resource_id == "vol-04dec132e120282f1"
    assert finding.resource_arn == (
        "arn:aws:ec2:us-east-1:111122223333:volume/vol-04dec132e120282f1"
    )
    # The region is only in the ARN and the metadata here, not on the flagged resource.
    assert finding.region == "us-east-1"
    assert finding.action_type == ACTION_MODIFY_STORAGE


def test_a_savings_percentage_is_never_read_as_an_amount(settings):
    """29.41% of a $17 volume is $5, and reporting it as $29.41 beggars the volume.

    These checks report the percentage in the column before the amount, so taking the first
    column whose heading mentions savings produced a figure larger than the whole resource.
    """
    mapping = {"support": FakeCostOptimizationHubBackedClient()}
    findings = NativeRecommendations(
        RoutingAwsContext(mapping, settings), NoteCollector()
    ).trusted_advisor()

    assert findings[0].estimated_monthly_savings == 5.0


def test_the_evidence_keeps_the_columns_worth_reading(settings):
    """Five columns of plumbing come before the action, the sizes, and the cost."""
    mapping = {"support": FakeCostOptimizationHubBackedClient()}
    findings = NativeRecommendations(
        RoutingAwsContext(mapping, settings), NoteCollector()
    ).trusted_advisor()

    labels = [e.label for e in findings[0].evidence]
    assert "Recommended Action" in labels
    assert "Current Resource Summary" in labels
    assert "Estimated Monthly Cost" in labels
    # Identifiers and status are already on the finding, and the figure is its headline.
    assert not {"Recommendation Id", "Resource ARN", "Status", "Estimated Monthly Savings"} & set(
        labels
    )


def test_trusted_advisor_rightsizing_merges_with_our_own_rule(settings):
    """Two sources describing one instance must not be counted twice."""
    arn = "arn:aws:ec2:us-east-1:111122223333:instance/i-0abc123def456789"
    ta = NativeRecommendations(
        RoutingAwsContext({"support": FakeLowUtilizationSupportClient()}, settings),
        NoteCollector(),
    ).trusted_advisor()
    ours = make_finding("rightsize_ec2", savings=1800.0, resource_arn=arn, action=ACTION_RIGHTSIZE)

    merged = merge_findings([*ta, ours])

    assert len(merged) == 1
    assert merged[0].confidence == "high"


@pytest.mark.parametrize(
    ("check_name", "action", "category"),
    [
        ("Low Utilization Amazon EC2 Instances", ACTION_RIGHTSIZE, "rightsizing"),
        (
            "Amazon EC2 cost optimization recommendations for instances",
            ACTION_RIGHTSIZE,
            "rightsizing",
        ),
        ("Amazon EBS over-provisioned volumes", ACTION_MODIFY_STORAGE, "storage"),
        (
            "Amazon EBS cost optimization recommendations for volumes",
            ACTION_MODIFY_STORAGE,
            "storage",
        ),
        ("S3 Incomplete Multipart Upload Abort Configuration", ACTION_MODIFY_STORAGE, "storage"),
        ("Unassociated Elastic IP Addresses", ACTION_RELEASE, "network"),
        ("Underutilized Amazon EBS Volumes", ACTION_RIGHTSIZE, "rightsizing"),
    ],
)
def test_each_cost_check_maps_to_the_change_it_actually_asks_for(check_name, action, category):
    """A check that lands on the wrong action stops de-duplication from working.

    Trusted Advisor's EBS checks ask for a smaller or cheaper volume. Reading them as
    deletions meant our own EBS findings never merged with them, and the same change was
    counted twice.
    """
    assert _trusted_advisor_action(check_name) == (action, category)


def test_missing_support_plan_produces_a_helpful_note(settings):
    mapping = {"support": FakeSupportClient(denied=True)}
    notes = NoteCollector()
    findings = NativeRecommendations(RoutingAwsContext(mapping, settings), notes).trusted_advisor()

    assert findings == []
    statuses = {n.status for n in notes.notes if n.capability == "trusted-advisor"}
    assert "not_enrolled" in statuses or "unavailable" in statuses


def test_collect_gathers_every_source(native):
    recommendations, _, _ = native
    findings = recommendations.collect(regions=["us-east-1"])
    assert {f.source for f in findings} == {
        "compute-optimizer",
        "cost-optimization-hub",
        "trusted-advisor",
    }


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"Estimated Monthly Savings": "$18.30"}, 18.30),
        ({"Estimated Monthly Savings": "1,234.56"}, 1234.56),
        ({"Savings": ""}, 0.0),
        ({"Region": "us-east-1"}, 0.0),
    ],
)
def test_savings_are_parsed_from_whichever_column_holds_them(fields, expected):
    assert _extract_savings(fields) == expected


def test_api_constants_are_humanized_without_breaking_acronyms():
    assert _humanize("CPUOverprovisioned") == "CPU Overprovisioned"
    assert _humanize("PurchaseSavingsPlans") == "Purchase Savings Plans"
    assert _humanize("EBSVolume") == "EBS Volume"
    assert _humanize("restart_needed") == "Restart needed"
    assert _humanize("") == ""
