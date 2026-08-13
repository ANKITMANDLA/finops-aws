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
        {
            "m5.4xlarge": "0.768",
            "m5.2xlarge": "0.384",
            "m5.xlarge": "0.192",
            "m7g.2xlarge": "0.3264",
            "m7g.xlarge": "0.1632",
            "m4.large": "0.10",
            "m7i.large": "0.09",
            "gp2": ("0.10", "USW2-EBS:VolumeUsage.gp2"),
            "gp3": ("0.08", "USW2-EBS:VolumeUsage.gp3"),
        }
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

    # List prices: gp2 at $0.10 and gp3 at $0.08 over 1000 GB.
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


# ------------------------------------------------------------------- efs rules


@pytest.fixture
def efs_pricing(tmp_path, settings):
    api = FakePricingClient(
        {
            "General Purpose": ("0.30", "USW2-TimedStorage-ByteHrs", "GB-Mo"),
            "Infrequent Access": ("0.025", "USW2-IATimedStorage-ByteHrs", "GB-Mo"),
            "Provisioned Throughput": ("6.00", "USW2-ProvisionedTP-MiBpsHrs", "MiBps-Mo"),
        }
    )
    return PricingClient(
        aws=FakeAwsContext(api, settings),
        notes=NoteCollector(),
        cache_path=tmp_path / "efs-pricing.json",
    )


def make_file_system(resource_id="fs-1", **kwargs):
    attributes = {
        "standard_gb": 200.0,
        "ia_gb": 0.0,
        "archive_gb": 0.0,
        "one_zone": False,
        "throughput_mode": "bursting",
        "provisioned_throughput_mibps": None,
        "mount_target_count": 2,
        "transition_to_ia": "AFTER_30_DAYS",
        "has_lifecycle_policy": True,
    } | kwargs.pop("attributes", {})
    attributes.setdefault("size_gb", attributes["standard_gb"] + attributes["ia_gb"])
    return make_resource(
        resource_id,
        resource_type="efs:file-system",
        service="EFS",
        state="available",
        attributes=attributes,
        **kwargs,
    )


def test_provisioned_throughput_is_trimmed_to_the_busiest_hour_plus_headroom(efs_pricing):
    file_system = make_file_system(
        attributes={"throughput_mode": "provisioned", "provisioned_throughput_mibps": 100.0},
        metrics={"efs_throughput_mibps_peak": 6.8, "efs_throughput_mibps_avg": 4.2},
    )

    findings = run_one("efs.overprovisioned_throughput", [file_system], efs_pricing)

    assert len(findings) == 1
    finding = findings[0]
    labels = {e.label: e.value for e in finding.evidence}
    assert labels["Suggested throughput"] == "11 MiB/s"
    # 200 GB of Standard includes 10 MiB/s, so 90 MiB/s is billable today and 1 after.
    assert finding.estimated_monthly_savings == pytest.approx(89 * 6.00)
    assert "--provisioned-throughput-in-mibps 11" in finding.remediation.cli


def test_throughput_matching_the_workload_is_left_alone(efs_pricing):
    busy = make_file_system(
        attributes={"throughput_mode": "provisioned", "provisioned_throughput_mibps": 100.0},
        metrics={"efs_throughput_mibps_peak": 78.0},
    )
    bursting = make_file_system("fs-2", metrics={"efs_throughput_mibps_peak": 1.0})
    unmeasured = make_file_system(
        "fs-3",
        attributes={"throughput_mode": "provisioned", "provisioned_throughput_mibps": 100.0},
    )

    assert (
        run_one("efs.overprovisioned_throughput", [busy, bursting, unmeasured], efs_pricing) == []
    )


def test_throughput_provisioned_under_its_own_baseline_is_not_a_saving(efs_pricing):
    # 400 GB in Standard already carries 20 MiB/s, so the 8 provisioned cost nothing.
    free = make_file_system(
        attributes={
            "standard_gb": 400.0,
            "throughput_mode": "provisioned",
            "provisioned_throughput_mibps": 8.0,
        },
        metrics={"efs_throughput_mibps_peak": 0.5},
    )

    assert run_one("efs.overprovisioned_throughput", [free], efs_pricing) == []


def test_a_file_system_with_no_tiering_is_flagged_at_the_difference_between_rates(efs_pricing):
    untiered = make_file_system(
        attributes={"standard_gb": 900.0, "transition_to_ia": None, "has_lifecycle_policy": False}
    )
    tiny = make_file_system(
        "fs-tiny",
        attributes={"standard_gb": 4.0, "transition_to_ia": None, "has_lifecycle_policy": False},
    )
    tiered = make_file_system("fs-tiered", attributes={"standard_gb": 900.0})

    findings = run_one("efs.no_lifecycle_policy", [untiered, tiny, tiered], efs_pricing)

    assert [f.resource_id for f in findings] == ["fs-1"]
    assert findings[0].estimated_monthly_savings == pytest.approx(900 * 0.5 * (0.30 - 0.025))
    assert findings[0].cost_basis == "heuristic"
    assert "put-lifecycle-configuration" in findings[0].remediation.cli


def test_a_file_system_nothing_mounts_is_flagged_for_deletion(efs_pricing):
    unreachable = make_file_system(
        attributes={"mount_target_count": 0}, monthly_cost=60.0, created_days_ago=200
    )
    never_connected = make_file_system(
        "fs-quiet", metrics={"efs_client_connections_max": 0.0}, monthly_cost=42.0
    )
    in_use = make_file_system(
        "fs-busy", metrics={"efs_client_connections_max": 18.0}, monthly_cost=42.0
    )
    # Nothing connects to a replication destination; AWS writes to it directly.
    replica = make_file_system(
        "fs-replica",
        attributes={"replication_overwrite_protection": "DISABLED"},
        metrics={"efs_client_connections_max": 0.0},
        monthly_cost=42.0,
    )
    new = make_file_system(
        "fs-new", metrics={"efs_client_connections_max": 0.0}, monthly_cost=42.0, created_days_ago=3
    )

    findings = run_one(
        "efs.unused_file_system",
        [unreachable, never_connected, in_use, replica, new],
        efs_pricing,
    )

    assert {f.resource_id for f in findings} == {"fs-1", "fs-quiet"}
    assert findings[0].action_type == "delete"
    assert all(f.rollback_possible is False for f in findings)


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


# ---------------------------------------------------------- connectivity rules


def test_an_idle_interface_endpoint_is_flagged_and_a_free_one_is_not(pricing):
    idle = make_resource(
        "vpce-1",
        resource_type="ec2:vpc-endpoint",
        service="VPC",
        state="available",
        attributes={
            "billable": True,
            "endpoint_type": "Interface",
            "network_interface_count": 2,
            "service_name": "com.amazonaws.us-east-1.secretsmanager",
            "vpc_id": "vpc-1",
        },
        metrics={"endpoint_bytes_per_day": 1_000.0, "endpoint_active_connections": 0.0},
        monthly_cost=14.60,
    )
    gateway = make_resource(
        "vpce-2",
        resource_type="ec2:vpc-endpoint",
        service="VPC",
        state="available",
        attributes={"billable": False, "endpoint_type": "Gateway"},
        metrics={"endpoint_bytes_per_day": 0.0},
        monthly_cost=0.0,
    )

    findings = run_one("vpce.idle", [idle, gateway], pricing)

    assert [f.resource_id for f in findings] == ["vpce-1"]
    assert findings[0].estimated_monthly_savings == 14.60
    assert "secretsmanager" in findings[0].title


def test_a_busy_endpoint_and_a_new_one_are_both_left_alone(pricing):
    busy = make_resource(
        "vpce-busy",
        resource_type="ec2:vpc-endpoint",
        service="VPC",
        state="available",
        attributes={"billable": True, "endpoint_type": "Interface"},
        metrics={"endpoint_bytes_per_day": 40.0 * 1024**3},
        monthly_cost=7.30,
    )
    fresh = make_resource(
        "vpce-new",
        resource_type="ec2:vpc-endpoint",
        service="VPC",
        state="available",
        attributes={"billable": True, "endpoint_type": "Interface"},
        metrics={"endpoint_bytes_per_day": 0.0},
        monthly_cost=7.30,
        created_days_ago=2,
    )

    assert run_one("vpce.idle", [busy, fresh], pricing) == []


def test_only_our_own_transit_gateway_attachments_are_flagged(pricing):
    """The attachment charge lands on whoever created it, not on the gateway's owner."""
    mine = make_resource(
        "tgw-attach-1",
        resource_type="ec2:transit-gateway-attachment",
        service="VPC",
        state="available",
        attributes={
            "owned_by_this_account": True,
            "attachment_kind": "vpc",
            "transit_gateway_id": "tgw-1",
            "resource_id": "vpc-1",
        },
        metrics={"tgw_bytes_per_day": 2_000.0},
        monthly_cost=36.50,
    )
    theirs = make_resource(
        "tgw-attach-2",
        resource_type="ec2:transit-gateway-attachment",
        service="VPC",
        state="available",
        attributes={"owned_by_this_account": False, "attachment_kind": "vpc"},
        metrics={"tgw_bytes_per_day": 0.0},
        monthly_cost=0.0,
    )

    findings = run_one("tgw.idle_attachment", [mine, theirs], pricing)

    assert [f.resource_id for f in findings] == ["tgw-attach-1"]
    assert findings[0].estimated_monthly_savings == 36.50


def test_a_vpn_with_every_tunnel_down_is_flagged_without_waiting(pricing):
    down = make_resource(
        "vpn-1",
        resource_type="ec2:vpn-connection",
        service="VPC",
        state="available",
        attributes={"tunnel_count": 2, "tunnels_up": 0, "customer_gateway_id": "cgw-1"},
        metrics={"vpn_bytes_per_day": 0.0},
        monthly_cost=36.50,
        created_days_ago=3,
    )
    healthy = make_resource(
        "vpn-2",
        resource_type="ec2:vpn-connection",
        service="VPC",
        state="available",
        attributes={"tunnel_count": 2, "tunnels_up": 2, "customer_gateway_id": "cgw-2"},
        metrics={"vpn_bytes_per_day": 8.0 * 1024**3},
        monthly_cost=36.50,
    )

    findings = run_one("vpn.unused", [down, healthy], pricing)

    assert [f.resource_id for f in findings] == ["vpn-1"]
    assert findings[0].confidence == "medium"


def test_a_disabled_private_ca_still_bills_and_is_flagged(pricing):
    authority = make_resource(
        "ca-1",
        resource_type="acm-pca:certificate-authority",
        service="Certificate Manager",
        state="DISABLED",
        attributes={"billable": True, "usage_mode": "GENERAL_PURPOSE"},
        monthly_cost=400.0,
    )
    active = make_resource(
        "ca-2",
        resource_type="acm-pca:certificate-authority",
        service="Certificate Manager",
        state="ACTIVE",
        attributes={"billable": True, "usage_mode": "GENERAL_PURPOSE"},
        monthly_cost=400.0,
    )

    findings = run_one("acm.private_ca_billing", [authority, active], pricing)

    assert [f.resource_id for f in findings] == ["ca-1"]
    assert findings[0].estimated_monthly_savings == 400.0


def test_a_key_awaiting_deletion_is_reported_as_still_billing(pricing):
    pending = make_resource(
        "key-1",
        resource_type="kms:key",
        service="KMS",
        state="PendingDeletion",
        attributes={"aliases": ["alias/old-app"], "deletion_date": "2026-09-01T00:00:00+00:00"},
        monthly_cost=1.0,
    )
    enabled = make_resource(
        "key-2",
        resource_type="kms:key",
        service="KMS",
        state="Enabled",
        attributes={"aliases": ["alias/app"]},
        monthly_cost=1.0,
    )

    findings = run_one("kms.pending_deletion", [pending, enabled], pricing)

    assert [f.resource_id for f in findings] == ["key-1"]
    assert findings[0].risk == "low"


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
    # Quiet on CPU and quiet on the network: switching it off saves everything, so shrinking
    # it is not the recommendation worth making.
    idle = make_resource(
        "i-idle",
        state="running",
        attributes={"instance_type": "m5.4xlarge"},
        metrics={"cpu_avg": 0.4, "cpu_p95": 1.0, "network_bytes_per_day": 1_000_000.0},
        monthly_cost=561.0,
    )
    assert run_one("ec2.underutilized_instance", [idle], pricing) == []


def test_a_quiet_cpu_with_no_traffic_data_is_resized_rather_than_left_to_no_one(pricing):
    # Without network figures nothing can be called idle, so the idle rule stays silent. A
    # resize is then the recommendation to make: conservative, reversible, and better than
    # leaving the instance with none of ours and only an AWS check's figure on it.
    unknown = make_resource(
        "i-no-network-data",
        state="running",
        attributes={"instance_type": "m5.4xlarge"},
        metrics={"cpu_avg": 0.4, "cpu_p95": 1.0},
        monthly_cost=561.0,
    )
    ctx = make_context([unknown], pricing)

    findings = run_rules(ctx, only=["ec2.idle_instance", "ec2.underutilized_instance"])

    assert [f.rule_id for f in findings] == ["ec2.underutilized_instance"]


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


def test_an_oversized_instance_gets_one_recommendation_not_two(pricing):
    # An under-utilized x86 instance qualifies for both rules. Raising both leaves two rival
    # figures on one node, which is what made the savings look inflated.
    oversized = make_resource(
        "i-oversized",
        state="running",
        attributes={
            "instance_type": "m5.4xlarge",
            "platform_details": "Linux/UNIX",
            "architecture": "x86_64",
        },
        metrics={"cpu_avg": 9.0, "cpu_p95": 12.0},
        monthly_cost=561.0,
    )
    ctx = make_context([oversized], pricing)

    findings = run_rules(ctx, only=["ec2.underutilized_instance", "ec2.graviton_candidate"])

    assert [f.rule_id for f in findings] == ["ec2.underutilized_instance"]
    # The ARM option is not lost: it is the resize finding's next step.
    graviton = next(e for e in findings[0].evidence if e.label == "Further on Graviton")
    assert "m7g.2xlarge" in graviton.value
    assert "arm64" in findings[0].detail


def test_a_quiet_cpu_moving_real_traffic_is_resized_rather_than_ignored(pricing):
    # An EKS node can sit at 1% CPU while forwarding gigabytes a day. Low CPU alone used to
    # send it to the idle rule, which then declined on the traffic, so neither rule spoke and
    # Trusted Advisor's "switch it off" figure was the only claim left on the instance.
    busy_network = make_resource(
        "i-network-bound",
        state="running",
        attributes={
            "instance_type": "m5.4xlarge",
            "platform_details": "Linux/UNIX",
            "architecture": "x86_64",
        },
        metrics={"cpu_avg": 0.6, "cpu_p95": 0.7, "network_bytes_per_day": 7_000_000_000.0},
        monthly_cost=561.0,
    )
    ctx = make_context([busy_network], pricing)

    findings = run_rules(
        ctx, only=["ec2.idle_instance", "ec2.underutilized_instance", "ec2.graviton_candidate"]
    )

    assert [f.rule_id for f in findings] == ["ec2.underutilized_instance"]
    assert findings[0].estimated_monthly_savings > 0
    assert findings[0].remediation is not None and findings[0].remediation.cli


def test_a_right_sized_instance_still_gets_the_graviton_recommendation(pricing):
    busy = make_resource(
        "i-busy",
        state="running",
        attributes={
            "instance_type": "m5.xlarge",
            "platform_details": "Linux/UNIX",
            "architecture": "x86_64",
        },
        metrics={"cpu_avg": 55.0, "cpu_p95": 78.0},
        monthly_cost=140.0,
    )
    ctx = make_context([busy], pricing)

    findings = run_rules(ctx, only=["ec2.underutilized_instance", "ec2.graviton_candidate"])

    assert [f.rule_id for f in findings] == ["ec2.graviton_candidate"]


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


def test_a_namespaced_owner_tag_still_names_an_owner(pricing):
    # Organizations prefix their tag keys. Reading "acme:business:owner" as untagged put
    # the entire estate in the unallocatable column.
    resources = [
        make_resource("i-1", tags={"acme:business:owner": "platform@acme.com"}, monthly_cost=100.0),
        make_resource("i-2", tags={"finance/cost-center": "0003"}, monthly_cost=100.0),
        make_resource("i-3", tags={"Owner_Email": "a@b.c"}, monthly_cost=100.0),
        make_resource("i-4", tags={"Name": "web-01", "Downtime": "no"}, monthly_cost=250.0),
    ]

    findings = run_one("governance.untagged_resources", resources, pricing)

    assert len(findings) == 1
    # Only the resource with nothing but a display name is unallocatable.
    assert "1 resources have no owner tag" in findings[0].title
    assert "250.00" in findings[0].title


def test_an_owner_word_inside_a_longer_word_is_not_an_owner_tag(pricing):
    resources = [make_resource("i-1", tags={"ownership-model": "shared"}, monthly_cost=100.0)]

    findings = run_one("governance.untagged_resources", resources, pricing)

    assert "1 resources have no owner tag" in findings[0].title


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
    # The finding that supplies the steps supplies the money, so the figure on screen is
    # the figure those steps would deliver. AWS supplied no commands, so ours leads.
    assert finding.remediation.cli.startswith("aws ec2 modify-instance-attribute")
    assert finding.estimated_monthly_savings == 100.0
    assert finding.source == "rules"
    labels = {e.label for e in finding.evidence}
    assert {"p95 CPU", "Compute Optimizer finding", "Corroborated by"} <= labels
    assert finding.confidence == "high"
    # AWS's own figure is not thrown away; it is quoted for what it assumes.
    quoted = next(e for e in finding.evidence if e.label == "AWS Compute Optimizer estimates")
    assert "$180" in quoted.value


def test_a_stop_it_estimate_never_gets_attached_to_a_resize_recommendation():
    # Trusted Advisor's low-utilization check prices terminating the instance; our rule
    # prices halving it. Quoting the first next to the second is how a $1,121 saving comes
    # to read as $2,212.
    arn = "arn:aws:ec2:us-west-2:111122223333:instance/i-low"
    ours = make_finding("ec2.underutilized_instance", savings=1121.28, resource_arn=arn)
    ours.action_type = "rightsize"
    ours.id = make_finding_id("rightsize", arn)
    ours.title = "Resize worker from m5.16xlarge to m5.8xlarge"
    ours.remediation = Remediation(summary="Resize it", cli="aws ec2 modify-instance-attribute ...")

    advisor = make_finding("trusted-advisor.Qch7DwouX1", savings=2211.84, resource_arn=arn)
    advisor.action_type = "rightsize"
    advisor.id = make_finding_id("rightsize", arn)
    advisor.source = "trusted-advisor"
    advisor.title = "Low Utilization Amazon EC2 Instances: i-low"
    advisor.remediation = Remediation(summary="Consider stopping the instance")
    advisor.evidence = [
        Evidence(label="Estimated Monthly Savings", value="$2211.84"),
        Evidence(label="Instance Type", value="m5.16xlarge"),
    ]

    finding = merge_findings([advisor, ours])[0]

    assert finding.estimated_monthly_savings == 1121.28
    assert finding.title == "Resize worker from m5.16xlarge to m5.8xlarge"
    quoted = next(e for e in finding.evidence if e.label == "AWS Trusted Advisor estimates")
    assert "$2,211.84" in quoted.value
    # Their bare figure is not copied in alongside ours, where it would read as a second and
    # contradictory answer to how much this change is worth.
    assert not any(e.label == "Estimated Monthly Savings" for e in finding.evidence)
    # Everything else they observed is still worth keeping.
    assert any(e.label == "Instance Type" for e in finding.evidence)


def test_different_actions_on_one_resource_stay_separate():
    arn = "arn:aws:ec2:us-east-1:111122223333:volume/vol-1"
    resize = make_finding("ebs.gp2_to_gp3", savings=20.0, resource_arn=arn)
    resize.id = make_finding_id("modify_storage", arn)
    delete = make_finding("ebs.unattached_volume", savings=100.0, resource_arn=arn)
    delete.id = make_finding_id("delete", arn)

    assert len(merge_findings([resize, delete])) == 2


def test_two_ways_to_save_on_one_resource_only_count_once():
    # Halving an instance and moving it to Graviton are both real and both worth doing, but
    # the second saves a share of what is left after the first. Adding them promises money
    # twice, so the larger claim counts and the other is marked.
    arn = "arn:aws:ec2:us-west-2:111122223333:instance/i-both"
    resize = make_finding("ec2.underutilized_instance", savings=1121.28, resource_arn=arn)
    resize.id = make_finding_id("rightsize", arn)
    resize.title = "Resize worker from m5.16xlarge to m5.8xlarge"
    graviton = make_finding("ec2.graviton_candidate", savings=336.38, resource_arn=arn)
    graviton.id = make_finding_id("migrate", arn)

    merged = merge_findings([resize, graviton])

    counted = [f for f in merged if f.counts_toward_total]
    assert [f.rule_id for f in counted] == ["ec2.underutilized_instance"]
    alternative = next(f for f in merged if not f.counts_toward_total)
    assert alternative.rule_id == "ec2.graviton_candidate"
    # It keeps its own figure and says which finding carries the money instead.
    assert alternative.estimated_monthly_savings == 336.38
    assert alternative.alternative_to == "Resize worker from m5.16xlarge to m5.8xlarge"


def test_findings_on_different_resources_all_count():
    findings = [
        make_finding("ec2.idle", savings=50.0, resource_arn="arn:a"),
        make_finding("ec2.idle", savings=60.0, resource_arn="arn:b"),
    ]
    findings[0].id = make_finding_id("stop", "arn:a")
    findings[1].id = make_finding_id("stop", "arn:b")

    assert all(f.counts_toward_total for f in merge_findings(findings))


def test_a_finding_claiming_nothing_is_not_called_an_alternative():
    # Governance findings sit alongside a priced finding on the same resource and claim $0,
    # so they cannot be double counting anything.
    arn = "arn:aws:ec2:us-west-2:111122223333:instance/i-untagged"
    priced = make_finding("ec2.underutilized_instance", savings=900.0, resource_arn=arn)
    priced.id = make_finding_id("rightsize", arn)
    untagged = make_finding("governance.untagged_expensive_resource", savings=0.0, resource_arn=arn)
    untagged.id = make_finding_id("tag", arn)
    untagged.category = "governance"

    merged = merge_findings([priced, untagged])

    assert all(f.alternative_to is None for f in merged)


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
