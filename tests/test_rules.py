from __future__ import annotations

from datetime import date

import pytest
from tests.factories import make_finding, make_resource
from tests.fakes import FakeAwsContext, FakePricingClient

from finops.aws.costs import CommitmentSummary, CostSnapshot
from finops.aws.errors import NoteCollector
from finops.aws.pricing import PricingClient
from finops.config import Thresholds
from finops.model import (
    ACTION_MODIFY_STORAGE,
    ACTION_RIGHTSIZE,
    Evidence,
    Remediation,
    make_finding_id,
)
from finops.rules import REGISTRY, RuleContext, build_rules, merge_findings, run_rules


@pytest.fixture
def pricing(tmp_path, settings):
    api = FakePricingClient(
        {"m5.4xlarge": "0.768", "m5.2xlarge": "0.384", "m4.large": "0.10", "m7i.large": "0.09"}
    )
    return PricingClient(
        aws=FakeAwsContext(api, settings),
        notes=NoteCollector(),
        cache_path=tmp_path / "pricing.json",
    )


def make_context(resources, pricing, *, cost=None, thresholds=None) -> RuleContext:
    return RuleContext(
        resources=list(resources),
        cost=cost or CostSnapshot(period_start=date(2026, 7, 1), period_end=date(2026, 7, 31)),
        pricing=pricing,
        thresholds=thresholds or Thresholds(),
    )


def run_one(rule_id, resources, pricing, **kwargs):
    ctx = make_context(resources, pricing, **kwargs)
    return run_rules(ctx, only=[rule_id])


# ------------------------------------------------------------------ idle rules


def test_idle_instance_is_flagged_with_its_evidence(pricing):
    instance = make_resource(
        "i-idle",
        state="running",
        attributes={"instance_type": "t3.large"},
        metrics={"cpu_avg": 1.2, "cpu_max": 4.0, "network_bytes_per_day": 1024.0},
        monthly_cost=60.0,
    )

    findings = run_one("ec2.idle_instance", [instance], pricing)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.estimated_monthly_savings == 60.0
    assert finding.action_type == "stop"
    assert finding.resource_id == "i-idle"
    labels = {e.label: e.value for e in finding.evidence}
    assert labels["Average CPU"] == "1.20%"
    assert "stop-instances" in finding.remediation.cli


def test_a_busy_instance_is_not_flagged_as_idle(pricing):
    busy = make_resource(
        "i-busy",
        state="running",
        attributes={"instance_type": "t3.large"},
        metrics={"cpu_avg": 45.0, "cpu_max": 90.0, "network_bytes_per_day": 5 * 1024**3},
    )
    assert run_one("ec2.idle_instance", [busy], pricing) == []


def test_an_instance_without_metrics_produces_no_finding(pricing):
    # No CloudWatch data means no evidence, and a finding with no evidence is a guess.
    unknown = make_resource("i-unknown", state="running", attributes={"instance_type": "t3.large"})
    assert run_one("ec2.idle_instance", [unknown], pricing) == []


def test_a_brand_new_instance_is_given_time_to_settle(pricing):
    fresh = make_resource(
        "i-fresh",
        state="running",
        created_days_ago=1,
        attributes={"instance_type": "t3.large"},
        metrics={"cpu_avg": 0.5, "network_bytes_per_day": 10.0},
    )
    assert run_one("ec2.idle_instance", [fresh], pricing) == []


def test_stopped_instance_is_charged_for_its_volumes(pricing):
    stopped = make_resource(
        "i-stopped",
        state="stopped",
        attributes={"instance_type": "m5.large", "attached_volume_ids": ["vol-a", "vol-b"]},
        monthly_cost=0.0,
    )
    volumes = [
        make_resource(
            "vol-a",
            resource_type="ebs:volume",
            service="EBS",
            state="in-use",
            attributes={"size_gb": 100, "volume_type": "gp3"},
            monthly_cost=8.0,
        ),
        make_resource(
            "vol-b",
            resource_type="ebs:volume",
            service="EBS",
            state="in-use",
            attributes={"size_gb": 500, "volume_type": "gp3"},
            monthly_cost=40.0,
        ),
    ]

    findings = run_one("ec2.stopped_instance_storage", [stopped, *volumes], pricing)

    assert len(findings) == 1
    assert findings[0].estimated_monthly_savings == 48.0
    assert findings[0].rollback_possible is False
    assert findings[0].risk == "high"


# --------------------------------------------------------------- storage rules


def test_unattached_volume_is_flagged_after_the_grace_period(pricing):
    old = make_resource(
        "vol-old",
        resource_type="ebs:volume",
        service="EBS",
        state="available",
        created_days_ago=60,
        attributes={"size_gb": 200, "volume_type": "gp2"},
        monthly_cost=20.0,
    )
    recent = make_resource(
        "vol-recent",
        resource_type="ebs:volume",
        service="EBS",
        state="available",
        created_days_ago=1,
        attributes={"size_gb": 200, "volume_type": "gp2"},
        monthly_cost=20.0,
    )

    findings = run_one("ebs.unattached_volume", [old, recent], pricing)

    assert [f.resource_id for f in findings] == ["vol-old"]
    assert findings[0].estimated_monthly_savings == 20.0


def test_gp2_to_gp3_uses_the_real_rate_difference(pricing):
    volume = make_resource(
        "vol-gp2",
        resource_type="ebs:volume",
        service="EBS",
        state="in-use",
        attributes={"size_gb": 1000, "volume_type": "gp2"},
        monthly_cost=100.0,
    )

    findings = run_one("ebs.gp2_to_gp3", [volume], pricing)

    # Fallback rates: gp2 at $0.10 and gp3 at $0.08 over 1000 GB.
    assert findings[0].estimated_monthly_savings == pytest.approx(20.0)
    assert findings[0].action_type == ACTION_MODIFY_STORAGE
    assert findings[0].risk == "low"
    assert "modify-volume" in findings[0].remediation.cli


def test_gp3_volumes_are_left_alone(pricing):
    volume = make_resource(
        "vol-gp3",
        resource_type="ebs:volume",
        service="EBS",
        attributes={"size_gb": 1000, "volume_type": "gp3"},
    )
    assert run_one("ebs.gp2_to_gp3", [volume], pricing) == []


def test_snapshot_backing_an_ami_is_never_proposed_for_deletion(pricing):
    snapshot = make_resource(
        "snap-1",
        resource_type="ebs:snapshot",
        service="EBS Snapshots",
        created_days_ago=400,
        attributes={"volume_id": "vol-gone", "volume_size_gb": 100},
        monthly_cost=5.0,
    )
    image = make_resource(
        "ami-1",
        resource_type="ec2:image",
        service="AMI",
        attributes={"snapshot_ids": ["snap-1"], "backing_size_gb": 100},
    )

    assert run_one("ebs.stale_snapshot", [snapshot, image], pricing) == []
    # Without the AMI, the same snapshot is fair game.
    assert len(run_one("ebs.stale_snapshot", [snapshot], pricing)) == 1


def test_snapshot_whose_volume_still_exists_is_kept(pricing):
    snapshot = make_resource(
        "snap-2",
        resource_type="ebs:snapshot",
        service="EBS Snapshots",
        created_days_ago=400,
        attributes={"volume_id": "vol-live", "volume_size_gb": 100},
        monthly_cost=5.0,
    )
    volume = make_resource("vol-live", resource_type="ebs:volume", service="EBS", state="in-use")
    assert run_one("ebs.stale_snapshot", [snapshot, volume], pricing) == []


def test_ami_in_use_by_an_instance_is_not_flagged(pricing):
    image = make_resource(
        "ami-live",
        resource_type="ec2:image",
        service="AMI",
        created_days_ago=400,
        attributes={"snapshot_ids": ["snap-x"], "backing_size_gb": 8},
    )
    snapshot = make_resource(
        "snap-x",
        resource_type="ebs:snapshot",
        service="EBS Snapshots",
        attributes={"volume_size_gb": 8},
        monthly_cost=1.0,
    )
    instance = make_resource("i-1", attributes={"image_id": "ami-live"})

    assert run_one("ami.unused", [image, snapshot, instance], pricing) == []
    assert len(run_one("ami.unused", [image, snapshot], pricing)) == 1


def test_log_group_without_retention_is_flagged_by_stored_volume(pricing):
    noisy = make_resource(
        "/aws/lambda/noisy",
        resource_type="logs:log-group",
        service="CloudWatch Logs",
        attributes={"retention_days": None, "stored_gb": 800.0, "never_expires": True},
        monthly_cost=24.0,
    )
    tiny = make_resource(
        "/aws/lambda/tiny",
        resource_type="logs:log-group",
        service="CloudWatch Logs",
        attributes={"retention_days": None, "stored_gb": 0.2, "never_expires": True},
        monthly_cost=0.01,
    )

    findings = run_one("logs.no_retention", [noisy, tiny], pricing)

    assert [f.resource_id for f in findings] == ["/aws/lambda/noisy"]
    assert findings[0].cost_basis == "heuristic"
    assert findings[0].estimated_monthly_savings == pytest.approx(24.0 * 0.7)


# --------------------------------------------------------------- network rules


def test_unassociated_elastic_ip_is_flagged(pricing):
    unattached = make_resource(
        "eipalloc-1",
        resource_type="ec2:elastic-ip",
        service="VPC",
        attributes={"associated": False, "public_ip": "1.2.3.4"},
        monthly_cost=3.65,
    )
    attached = make_resource(
        "eipalloc-2",
        resource_type="ec2:elastic-ip",
        service="VPC",
        attributes={"associated": True, "public_ip": "5.6.7.8"},
        monthly_cost=3.65,
    )

    findings = run_one("eip.unassociated", [unattached, attached], pricing)

    assert [f.resource_id for f in findings] == ["eipalloc-1"]
    assert findings[0].rollback_possible is False


def test_load_balancer_with_no_healthy_targets_is_flagged(pricing):
    balancer = make_resource(
        "public-alb",
        resource_type="elbv2:application",
        service="ELB",
        attributes={"target_count": 3, "healthy_target_count": 0, "lb_type": "application"},
        monthly_cost=18.0,
    )

    findings = run_one("elb.no_healthy_targets", [balancer], pricing)

    assert findings[0].estimated_monthly_savings == 18.0
    assert findings[0].risk == "high"


def test_idle_rule_defers_to_the_more_specific_unhealthy_rule(pricing):
    balancer = make_resource(
        "dead-alb",
        resource_type="elbv2:application",
        service="ELB",
        attributes={"target_count": 2, "healthy_target_count": 0, "lb_type": "application"},
        metrics={"requests_per_day": 0.0},
        monthly_cost=18.0,
    )
    assert run_one("elb.idle", [balancer], pricing) == []


# ------------------------------------------------------------ rightsizing rules


def test_underutilized_instance_is_priced_from_the_smaller_type(pricing):
    instance = make_resource(
        "i-big",
        state="running",
        attributes={"instance_type": "m5.4xlarge", "platform_details": "Linux/UNIX"},
        metrics={"cpu_avg": 12.0, "cpu_p95": 18.0},
        monthly_cost=561.0,
    )

    findings = run_one("ec2.underutilized_instance", [instance], pricing)

    assert "m5.2xlarge" in findings[0].title
    # 561 now versus the m5.2xlarge list price of 0.384 * 730.
    assert findings[0].estimated_monthly_savings == pytest.approx(561.0 - 0.384 * 730, abs=0.1)
    assert findings[0].action_type == ACTION_RIGHTSIZE


def test_idle_instances_are_left_to_the_idle_rule(pricing):
    idle = make_resource(
        "i-idle",
        state="running",
        attributes={"instance_type": "m5.4xlarge"},
        metrics={"cpu_avg": 0.4, "cpu_p95": 1.0},
        monthly_cost=561.0,
    )
    assert run_one("ec2.underutilized_instance", [idle], pricing) == []


def test_previous_generation_instance_is_upgraded(pricing):
    instance = make_resource(
        "i-old",
        state="running",
        attributes={"instance_type": "m4.large", "platform_details": "Linux/UNIX"},
        metrics={"cpu_avg": 30.0, "cpu_p95": 60.0},
        monthly_cost=73.0,
    )

    findings = run_one("ec2.previous_generation", [instance], pricing)

    assert "m7i.large" in findings[0].title
    assert findings[0].estimated_monthly_savings == pytest.approx(73.0 - 0.09 * 730, abs=0.1)


def test_windows_instances_are_not_offered_graviton(pricing):
    windows = make_resource(
        "i-win",
        state="running",
        attributes={
            "instance_type": "m5.xlarge",
            "platform_details": "Windows",
            "architecture": "x86_64",
        },
        monthly_cost=300.0,
    )
    assert run_one("ec2.graviton_candidate", [windows], pricing) == []


def test_linux_instance_gets_a_graviton_suggestion_marked_high_effort(pricing):
    linux = make_resource(
        "i-lin",
        state="running",
        attributes={
            "instance_type": "m5.xlarge",
            "platform_details": "Linux/UNIX",
            "architecture": "x86_64",
        },
        monthly_cost=140.0,
    )

    findings = run_one("ec2.graviton_candidate", [linux], pricing)

    assert "m7g.xlarge" in findings[0].title
    assert findings[0].implementation_effort == "high"
    assert findings[0].confidence == "low"


def test_overprovisioned_dynamo_table_is_flagged(pricing):
    table = make_resource(
        "orders",
        resource_type="dynamodb:table",
        service="DynamoDB",
        attributes={
            "billing_mode": "PROVISIONED",
            "read_capacity_units": 1000,
            "write_capacity_units": 1000,
        },
        metrics={"read_utilization_percent": 2.0, "write_utilization_percent": 1.0},
        monthly_cost=569.0,
    )

    findings = run_one("dynamodb.overprovisioned_capacity", [table], pricing)

    assert findings[0].estimated_monthly_savings > 400
    assert "PAY_PER_REQUEST" in findings[0].remediation.cli


# -------------------------------------------------------------- database rules


def test_read_replica_with_no_connections_is_flagged(pricing):
    replica = make_resource(
        "orders-replica",
        resource_type="rds:db-instance",
        service="RDS",
        state="available",
        attributes={
            "read_replica_source": "orders",
            "instance_class": "db.r5.xlarge",
            "engine": "postgres",
        },
        metrics={"db_connections_max": 0.0},
        monthly_cost=380.0,
    )

    findings = run_one("rds.unused_read_replica", [replica], pricing)

    assert findings[0].estimated_monthly_savings == 380.0
    assert "orders" in findings[0].detail


# ------------------------------------------------------------ commitment rules


def test_savings_plan_recommendation_becomes_a_finding(pricing):
    cost = CostSnapshot(
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        commitments=CommitmentSummary(
            savings_plans_coverage_percent=10.0,
            savings_plans_recommendation={
                "estimated_monthly_savings": 320.0,
                "estimated_savings_percentage": 21.0,
                "hourly_commitment": 1.25,
                "current_on_demand_spend": 1500.0,
                "term": "1 year, no upfront, Compute Savings Plan",
            },
        ),
    )

    findings = run_one("commitments.savings_plan_gap", [], pricing, cost=cost)

    assert findings[0].estimated_monthly_savings == 320.0
    assert findings[0].cost_basis == "aws_recommendation"
    assert findings[0].rollback_possible is False
    assert findings[0].resource_arn is None


def test_coverage_gap_is_suppressed_when_aws_already_recommends_a_plan(pricing):
    with_recommendation = CostSnapshot(
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        total_cost=3000.0,
        commitments=CommitmentSummary(
            savings_plans_coverage_percent=10.0,
            savings_plans_recommendation={"estimated_monthly_savings": 320.0},
        ),
    )
    assert run_one("commitments.low_coverage", [], pricing, cost=with_recommendation) == []


def test_underused_commitment_is_flagged(pricing):
    cost = CostSnapshot(
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        total_cost=3000.0,
        commitments=CommitmentSummary(
            savings_plans_utilization_percent=70.0,
            savings_plans_coverage_percent=50.0,
        ),
    )

    findings = run_one("commitments.low_utilization", [], pricing, cost=cost)

    assert findings[0].title.startswith("Savings Plans are only 70.0% used")
    assert findings[0].estimated_monthly_savings > 0


# ------------------------------------------------------------ governance rules


def test_untagged_resources_are_reported_without_claiming_savings(pricing):
    resources = [
        make_resource("i-1", tags={"Owner": "platform"}, monthly_cost=100.0),
        make_resource("i-2", tags={}, monthly_cost=250.0),
        make_resource("i-3", tags={"aws:autoscaling:groupName": "asg"}, monthly_cost=50.0),
    ]

    findings = run_one("governance.untagged_resources", resources, pricing)

    assert len(findings) == 1
    finding = findings[0]
    # An AWS-managed tag is not ownership.
    assert "2 resources have no owner tag" in finding.title
    assert finding.estimated_monthly_savings == 0.0
    assert "300.00" in finding.title


def test_expensive_untagged_resources_are_called_out_individually(pricing):
    resources = [
        make_resource("i-costly", tags={}, monthly_cost=900.0),
        make_resource("i-cheap", tags={}, monthly_cost=12.0),
    ]

    findings = run_one("governance.untagged_expensive_resource", resources, pricing)

    assert [f.resource_id for f in findings] == ["i-costly"]


# ---------------------------------------------------------------- engine level


def test_every_registered_rule_can_run_against_an_empty_account(pricing):
    ctx = make_context([], pricing)
    assert run_rules(ctx) == []
    assert len(build_rules()) == len(REGISTRY)


def test_a_broken_rule_does_not_stop_the_others(pricing, monkeypatch):
    from finops.rules.base import Rule as BaseRule

    class ExplodingRule(BaseRule):
        id = "test.exploding"
        category = "idle"

        def evaluate(self, ctx):
            raise RuntimeError("boom")

    monkeypatch.setitem(REGISTRY, "test.exploding", ExplodingRule)
    instance = make_resource(
        "i-idle",
        state="running",
        attributes={"instance_type": "t3.large"},
        metrics={"cpu_avg": 0.5, "network_bytes_per_day": 100.0},
        monthly_cost=60.0,
    )

    ctx = make_context([instance], pricing)
    findings = run_rules(ctx, only=["test.exploding", "ec2.idle_instance"])

    assert [f.rule_id for f in findings] == ["ec2.idle_instance"]


# --------------------------------------------------------------------- merging


def test_duplicate_findings_from_two_sources_are_merged_once():
    arn = "arn:aws:ec2:us-east-1:111122223333:instance/i-dup"
    ours = make_finding("ec2.underutilized_instance", savings=100.0, resource_arn=arn)
    ours.action_type = "rightsize"
    ours.id = make_finding_id("rightsize", arn)
    ours.remediation = Remediation(summary="Resize it", cli="aws ec2 modify-instance-attribute ...")
    ours.evidence = [Evidence(label="p95 CPU", value="12%")]

    aws = make_finding("compute-optimizer.ec2_instance", savings=180.0, resource_arn=arn)
    aws.action_type = "rightsize"
    aws.id = make_finding_id("rightsize", arn)
    aws.source = "compute-optimizer"
    aws.cost_basis = "aws_recommendation"
    aws.remediation = Remediation(summary="Change to m5.2xlarge")
    aws.evidence = [Evidence(label="Compute Optimizer finding", value="Overprovisioned")]

    merged = merge_findings([ours, aws])

    assert len(merged) == 1
    finding = merged[0]
    # AWS's estimate wins because it is based on billing data we cannot see.
    assert finding.estimated_monthly_savings == 180.0
    assert finding.source == "compute-optimizer"
    # But our runnable command survives, since AWS supplied none.
    assert finding.remediation.cli.startswith("aws ec2 modify-instance-attribute")
    labels = {e.label for e in finding.evidence}
    assert {"p95 CPU", "Compute Optimizer finding", "Corroborated by"} <= labels
    assert finding.confidence == "high"


def test_different_actions_on_one_resource_stay_separate():
    arn = "arn:aws:ec2:us-east-1:111122223333:volume/vol-1"
    resize = make_finding("ebs.gp2_to_gp3", savings=20.0, resource_arn=arn)
    resize.id = make_finding_id("modify_storage", arn)
    delete = make_finding("ebs.unattached_volume", savings=100.0, resource_arn=arn)
    delete.id = make_finding_id("delete", arn)

    assert len(merge_findings([resize, delete])) == 2


def test_findings_are_ranked_and_small_ones_dropped():
    findings = [
        make_finding("a", savings=5.0, resource_arn="arn:1"),
        make_finding("b", savings=500.0, resource_arn="arn:2"),
        make_finding("c", savings=0.10, resource_arn="arn:3"),
    ]

    merged = merge_findings(findings, min_savings=1.0)

    assert [f.estimated_monthly_savings for f in merged] == [500.0, 5.0]


def test_governance_findings_survive_the_savings_filter():
    governance = make_finding("governance.untagged_resources", savings=0.0, resource_arn="arn:g")
    governance.category = "governance"

    merged = merge_findings([governance], min_savings=25.0)

    assert [f.rule_id for f in merged] == ["governance.untagged_resources"]
