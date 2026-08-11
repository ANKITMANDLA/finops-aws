"""Offline demo scan.

``finops scan --dry-run`` builds a representative account inside moto, runs the real
collectors, rules, and report against it, and stores the result like any other scan. It
exists so the pipeline and the dashboard can be exercised without AWS credentials and
without spending Cost Explorer requests.

Two things here are simulated rather than collected, and both are labelled as such in the
scan: the cost figures (moto has no Cost Explorer) and the utilization metrics (moto has
no CloudWatch history). Everything else - inventory, pricing fallbacks, rules, findings,
de-duplication, the TCO report - is the same code that runs against a real account.
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from typing import Any

from finops.agent.advisor import Advisor, build_advisor
from finops.attribution import attribute_costs
from finops.aws.collectors import CollectionContext, collect_inventory
from finops.aws.costs import CommitmentSummary, CostSnapshot
from finops.aws.errors import NoteCollector
from finops.aws.pricing import PricingClient
from finops.aws.session import AwsContext
from finops.config import Settings
from finops.model import CostRecord, Resource, Scan, utcnow
from finops.pipeline import _new_scan_id
from finops.rules import RuleContext, merge_findings, run_rules
from finops.store import ScanStore
from finops.tco import build_tco_report, rank_findings

logger = logging.getLogger(__name__)

DEMO_REGIONS = ("us-east-1", "eu-west-1")

# Plausible on-demand rates so the demo produces believable dollar figures. Real scans
# read these from the Price List API.
_SIZE_HOURLY = {
    "nano": 0.0052,
    "micro": 0.0104,
    "small": 0.0208,
    "medium": 0.0416,
    "large": 0.0832,
    "xlarge": 0.1664,
    "2xlarge": 0.3328,
    "4xlarge": 0.6656,
    "8xlarge": 1.3312,
    "12xlarge": 1.9968,
    "16xlarge": 2.6624,
}

_FAMILY_FACTOR = {"t": 0.55, "m": 1.0, "c": 0.92, "r": 1.32, "i": 1.7, "x": 2.6, "d": 1.9}


class DemoPricingClient(PricingClient):
    """Prices instances from a table instead of the Price List API.

    Everything else falls through to ``PricingClient``'s static fallbacks, so storage,
    NAT, and load balancer estimates come from the same numbers a credential-less real
    scan would use.
    """

    def _query(self, service_code: str, filters: dict[str, str]) -> float | None:
        instance_type = filters.get("instanceType")
        if not instance_type:
            return None
        if service_code == "AmazonEC2":
            return _instance_hourly(instance_type)
        if service_code == "AmazonRDS":
            # Managed databases carry a premium over raw compute.
            base = _instance_hourly(instance_type.removeprefix("db."))
            return round(base * 2.1, 5) if base else None
        return None


def _instance_hourly(instance_type: str) -> float | None:
    family, _, size = instance_type.partition(".")
    hourly = _SIZE_HOURLY.get(size)
    if hourly is None or not family:
        return None
    factor = _FAMILY_FACTOR.get(family[0], 1.0)
    # Graviton families (m6g, c7g, r6g) list roughly 20% below their x86 siblings.
    if family[-1] == "g" and len(family) > 2:
        factor *= 0.8
    return round(hourly * factor, 5)


def run_demo_scan(
    settings: Settings,
    *,
    store: ScanStore | None = None,
    persist: bool = True,
    with_advice: bool = True,
    advisor: Advisor | None = None,
    progress=None,
) -> Scan:
    """Run the full pipeline against a mocked account."""
    try:
        from moto import mock_aws
    except ImportError as exc:  # pragma: no cover - depends on the install extras
        raise RuntimeError(
            "The dry run needs moto. Install the dev extras: pip install -e '.[dev]'"
        ) from exc

    started = time.monotonic()

    def step(stage: str, message: str) -> None:
        logger.info("[%s] %s", stage, message)
        if progress:
            progress(stage, message)

    demo_settings = settings.model_copy(update={"aws_profile": None, "regions": list(DEMO_REGIONS)})

    with mock_aws():
        _fake_credentials()
        seed_account()
        notes = NoteCollector()
        aws = AwsContext(settings=demo_settings)

        step("inventory", f"Collecting from the mock account in {len(DEMO_REGIONS)} regions")
        ctx = CollectionContext(aws=aws, notes=notes, target_regions=list(DEMO_REGIONS))
        resources = collect_inventory(ctx, regions=list(DEMO_REGIONS))
        resources = _remove_mock_artifacts(resources)
        step("inventory", f"Found {len(resources)} resources")

        _age_resources(resources)
        _apply_demo_metrics(resources)
        notes.add(
            "cloudwatch:GetMetricData",
            "unavailable",
            "Dry run: utilization metrics are simulated, not collected.",
        )

        snapshot = demo_cost_snapshot(settings.cost_lookback_days)
        notes.add(
            "ce:GetCostAndUsage",
            "unavailable",
            "Dry run: cost figures are synthetic. Run a real scan for billed amounts.",
        )

        step("pricing", "Attributing cost to resources")
        pricing = DemoPricingClient(aws, notes)
        attribute_costs(resources, snapshot, pricing)

        step("rules", f"Evaluating rules against {len(resources)} resources")
        findings = run_rules(
            RuleContext(
                resources=resources,
                cost=snapshot,
                pricing=pricing,
                thresholds=settings.thresholds,
            )
        )
        findings = rank_findings(
            merge_findings(findings, min_savings=settings.thresholds.min_monthly_savings_usd)
        )
        step("rules", f"{len(findings)} finding(s)")

        scan = Scan(
            scan_id=_new_scan_id(),
            account_id=aws.account_id,
            account_alias="demo (dry run)",
            started_at=utcnow(),
            regions=list(DEMO_REGIONS),
            resources=resources,
            costs=snapshot.records,
            findings=findings,
            notes=notes.notes,
            dry_run=True,
        )
        scan.tco = build_tco_report(snapshot, findings, resources)

    # Advice runs outside the mock so a real Bedrock or API key still works.
    if with_advice:
        step("advice", "Generating architectural recommendations")
        advisor = advisor or build_advisor(settings, AwsContext(settings=settings))
        scan.advice = advisor.advise(scan.tco, findings, resources, scan.notes)

    scan.finished_at = utcnow()
    scan.duration_seconds = round(time.monotonic() - started, 2)

    if persist:
        (store or ScanStore(settings.db_path)).save_scan(scan)
    return scan


def _fake_credentials() -> None:
    import os

    os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
    os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
    os.environ.setdefault("AWS_DEFAULT_REGION", DEMO_REGIONS[0])


# --------------------------------------------------------------------- seeding


def seed_account() -> None:
    """Create a small but messy estate: the kind of account that has savings in it."""
    import boto3

    _seed_primary_region(boto3, DEMO_REGIONS[0])
    _seed_secondary_region(boto3, DEMO_REGIONS[1])


def _tags(**pairs: str) -> list[dict[str, str]]:
    return [{"Key": key, "Value": value} for key, value in pairs.items()]


def _seed_primary_region(boto3, region: str) -> None:
    ec2 = boto3.client("ec2", region_name=region)
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    subnets = [
        ec2.create_subnet(
            VpcId=vpc, CidrBlock=f"10.0.{index}.0/24", AvailabilityZone=f"{region}{letter}"
        )["Subnet"]["SubnetId"]
        for index, letter in enumerate("abc", start=1)
    ]
    image_id = ec2.describe_images()["Images"][0]["ImageId"]

    fleet: list[tuple[str, str, dict[str, str]]] = [
        ("web-1", "m5.xlarge", {"Name": "web-1", "env": "prod", "owner": "platform"}),
        ("api-1", "m5.2xlarge", {"Name": "api-1", "env": "prod", "owner": "payments"}),
        ("batch-1", "c5.4xlarge", {"Name": "batch-1", "env": "prod", "owner": "data"}),
        ("legacy-1", "m3.large", {"Name": "legacy-1", "env": "prod"}),
        ("analytics-1", "r5.2xlarge", {"Name": "analytics-1", "env": "prod", "owner": "data"}),
    ]
    for _, instance_type, tags in fleet:
        ec2.run_instances(
            ImageId=image_id,
            MinCount=1,
            MaxCount=1,
            InstanceType=instance_type,
            SubnetId=subnets[0],
            TagSpecifications=[{"ResourceType": "instance", "Tags": _tags(**tags)}],
        )

    # Stopped, but its 200 GB root volume is still billed every hour.
    stopped = ec2.run_instances(
        ImageId=image_id,
        MinCount=1,
        MaxCount=1,
        InstanceType="m5.large",
        SubnetId=subnets[1],
        TagSpecifications=[
            {"ResourceType": "instance", "Tags": _tags(Name="staging-old", env="staging")}
        ],
    )["Instances"][0]["InstanceId"]
    parked_volume = ec2.create_volume(AvailabilityZone=f"{region}a", Size=200, VolumeType="gp2")[
        "VolumeId"
    ]
    ec2.attach_volume(Device="/dev/sdf", InstanceId=stopped, VolumeId=parked_volume)
    ec2.stop_instances(InstanceIds=[stopped])

    # Two orphaned volumes and an over-provisioned io1.
    for size, volume_type, name in ((500, "gp2", "orphan-data"), (120, "gp3", "old-scratch")):
        ec2.create_volume(
            AvailabilityZone=f"{region}a",
            Size=size,
            VolumeType=volume_type,
            TagSpecifications=[{"ResourceType": "volume", "Tags": _tags(Name=name)}],
        )
    ec2.create_volume(
        AvailabilityZone=f"{region}a",
        Size=300,
        VolumeType="io1",
        Iops=12000,
        TagSpecifications=[{"ResourceType": "volume", "Tags": _tags(Name="db-data")}],
    )

    # Snapshots that nobody remembers taking.
    for index in range(3):
        volume = ec2.create_volume(AvailabilityZone=f"{region}a", Size=100, VolumeType="gp2")
        ec2.create_snapshot(
            VolumeId=volume["VolumeId"],
            Description=f"pre-migration-{index}",
            TagSpecifications=[
                {"ResourceType": "snapshot", "Tags": _tags(Name=f"pre-migration-{index}")}
            ],
        )

    # An idle NAT Gateway plus two Elastic IPs attached to nothing.
    nat_address = ec2.allocate_address(Domain="vpc")
    ec2.create_nat_gateway(SubnetId=subnets[0], AllocationId=nat_address["AllocationId"])
    ec2.allocate_address(Domain="vpc")
    ec2.allocate_address(Domain="vpc")

    elbv2 = boto3.client("elbv2", region_name=region)
    for name, lb_type in (("public-alb", "application"), ("internal-nlb", "network")):
        elbv2.create_load_balancer(
            Name=name,
            Subnets=subnets[:2],
            Type=lb_type,
            Tags=_tags(env="prod", owner="platform"),
        )
    # This one has no targets left behind it at all.
    elbv2.create_load_balancer(Name="legacy-alb", Subnets=subnets[:2], Type="application")

    rds = boto3.client("rds", region_name=region)
    rds.create_db_instance(
        DBInstanceIdentifier="orders-prod",
        DBInstanceClass="db.m5.2xlarge",
        Engine="postgres",
        AllocatedStorage=500,
        StorageType="gp2",
        BackupRetentionPeriod=7,
        Tags=_tags(env="prod", owner="payments"),
    )
    rds.create_db_instance(
        DBInstanceIdentifier="reporting-idle",
        DBInstanceClass="db.r5.xlarge",
        Engine="mysql",
        AllocatedStorage=200,
        StorageType="gp2",
        Tags=_tags(env="analytics"),
    )

    s3 = boto3.client("s3", region_name=region)
    for bucket in ("finops-demo-logs", "finops-demo-backups", "finops-demo-artifacts"):
        s3.create_bucket(Bucket=bucket)
    s3.put_bucket_versioning(
        Bucket="finops-demo-backups", VersioningConfiguration={"Status": "Enabled"}
    )

    ddb = boto3.client("dynamodb", region_name=region)
    ddb.create_table(
        TableName="sessions",
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
        ProvisionedThroughput={"ReadCapacityUnits": 200, "WriteCapacityUnits": 200},
        Tags=_tags(env="prod"),
    )

    logs = boto3.client("logs", region_name=region)
    for group in ("/aws/lambda/checkout", "/aws/eks/prod/cluster", "/application/legacy"):
        logs.create_log_group(logGroupName=group)
    logs.put_retention_policy(logGroupName="/aws/lambda/checkout", retentionInDays=30)

    _seed_lambda(boto3, region)
    _seed_eks(boto3, region, subnets)


def _seed_secondary_region(boto3, region: str) -> None:
    ec2 = boto3.client("ec2", region_name=region)
    vpc = ec2.create_vpc(CidrBlock="10.10.0.0/16")["Vpc"]["VpcId"]
    subnets = [
        ec2.create_subnet(
            VpcId=vpc, CidrBlock=f"10.10.{index}.0/24", AvailabilityZone=f"{region}{letter}"
        )["Subnet"]["SubnetId"]
        for index, letter in enumerate("ab", start=1)
    ]
    image_id = ec2.describe_images()["Images"][0]["ImageId"]

    ec2.run_instances(
        ImageId=image_id,
        MinCount=1,
        MaxCount=1,
        InstanceType="t3.large",
        SubnetId=subnets[0],
        TagSpecifications=[
            {"ResourceType": "instance", "Tags": _tags(Name="eu-web-1", env="prod")}
        ],
    )
    # Nobody has logged into this since the pilot.
    ec2.run_instances(
        ImageId=image_id,
        MinCount=1,
        MaxCount=1,
        InstanceType="m5.large",
        SubnetId=subnets[0],
        TagSpecifications=[{"ResourceType": "instance", "Tags": _tags(Name="eu-pilot")}],
    )
    ec2.create_volume(AvailabilityZone=f"{region}a", Size=250, VolumeType="gp2")

    nat_address = ec2.allocate_address(Domain="vpc")
    ec2.create_nat_gateway(SubnetId=subnets[0], AllocationId=nat_address["AllocationId"])

    _seed_eks(boto3, region, subnets, cluster_name="eu-small")

    logs = boto3.client("logs", region_name=region)
    logs.create_log_group(logGroupName="/application/eu-legacy")


def _seed_lambda(boto3, region: str) -> None:
    import io
    import zipfile

    iam = boto3.client("iam", region_name=region)
    try:
        role = iam.create_role(
            RoleName="finops-demo-lambda",
            AssumeRolePolicyDocument='{"Version":"2012-10-17","Statement":[]}',
        )["Role"]["Arn"]
    except Exception:  # pragma: no cover - role already exists on a second call
        role = iam.get_role(RoleName="finops-demo-lambda")["Role"]["Arn"]

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("index.py", "def handler(event, context):\n    return True\n")
    code = buffer.getvalue()

    awslambda = boto3.client("lambda", region_name=region)
    functions = [
        ("checkout", 3008, "python3.11", "x86_64"),
        ("thumbnailer", 1024, "python3.11", "x86_64"),
        ("cron-cleanup", 512, "python3.11", "arm64"),
    ]
    for name, memory, runtime, architecture in functions:
        try:
            awslambda.create_function(
                FunctionName=name,
                Runtime=runtime,
                Role=role,
                Handler="index.handler",
                Code={"ZipFile": code},
                MemorySize=memory,
                Timeout=60,
                Architectures=[architecture],
                Tags={"env": "prod"},
            )
        except Exception as exc:  # pragma: no cover - moto lambda is fussy about IAM
            logger.debug("Skipping demo lambda %s: %s", name, exc)


def _seed_eks(boto3, region: str, subnets: list[str], cluster_name: str = "prod") -> None:
    eks = boto3.client("eks", region_name=region)
    try:
        eks.create_cluster(
            name=cluster_name,
            roleArn=f"arn:aws:iam::123456789012:role/eks-{cluster_name}",
            resourcesVpcConfig={"subnetIds": subnets},
            tags={"env": "prod"},
        )
        eks.create_nodegroup(
            clusterName=cluster_name,
            nodegroupName="default",
            subnets=subnets,
            nodeRole=f"arn:aws:iam::123456789012:role/eks-node-{cluster_name}",
            scalingConfig={"minSize": 2, "maxSize": 6, "desiredSize": 3},
            instanceTypes=["m5.xlarge"],
            capacityType="ON_DEMAND",
        )
    except Exception as exc:  # pragma: no cover - depends on the moto version
        logger.debug("Skipping demo EKS in %s: %s", region, exc)


# ------------------------------------------------------- simulated observations


def _remove_mock_artifacts(resources: list[Resource]) -> list[Resource]:
    """Drop resources moto invents on its own.

    moto pre-populates every region with thousands of snapshots and attributes them to
    the calling account, which would swamp the demo inventory. Only the snapshots this
    module created carry a ``pre-migration`` description.
    """
    kept: list[Resource] = []
    for resource in resources:
        if resource.resource_type == "ebs:snapshot":
            description = str(resource.attributes.get("description", ""))
            if not description.startswith("pre-migration"):
                continue
        kept.append(resource)
    return kept


def _age_resources(resources: list[Resource]) -> None:
    """Backdate some resources so age-based rules have something to see.

    Everything moto creates is a few milliseconds old, which would hide the orphaned
    volume, stale snapshot, and unused AMI rules behind their grace periods.
    """
    now = utcnow()
    ages = {
        "ebs:volume": 120,
        "ebs:snapshot": 200,
        "ec2:image": 240,
        "ec2:instance": 400,
        "rds:db-instance": 380,
        "rds:snapshot": 260,
        "eks:cluster": 300,
        "s3:bucket": 500,
    }
    for resource in resources:
        days = ages.get(resource.resource_type)
        if days:
            resource.created_at = now - timedelta(days=days)


def _apply_demo_metrics(resources: list[Resource]) -> None:
    """Attach plausible utilization so the rules have signals to act on.

    The mix is deliberate: a couple of genuinely busy resources that must not be flagged,
    and a few obviously idle ones that must be.
    """
    gb = 1024**3
    for resource in resources:
        name = (resource.name or resource.resource_id).lower()
        # A stable pseudo-random pick so repeated dry runs tell the same story.
        variant = sum(resource.resource_id.encode()) % 3

        if resource.resource_type == "ec2:instance":
            if "web" in name:
                resource.metrics = {
                    "cpu_avg": 46.0,
                    "cpu_p95": 71.0,
                    "cpu_max": 88.0,
                    "network_bytes_per_day": 240 * gb,
                }
            elif "api" in name or "pilot" in name:
                resource.metrics = {
                    "cpu_avg": 1.4,
                    "cpu_p95": 3.1,
                    "cpu_max": 6.0,
                    "network_bytes_per_day": 1.2 * 1024 * 1024,
                }
            elif "batch" in name or "analytics" in name:
                resource.metrics = {
                    "cpu_avg": 12.0,
                    "cpu_p95": 22.0,
                    "cpu_max": 54.0,
                    "network_bytes_per_day": 40 * gb,
                }
            else:
                resource.metrics = {
                    "cpu_avg": 22.0,
                    "cpu_p95": 38.0,
                    "cpu_max": 61.0,
                    "network_bytes_per_day": 12 * gb,
                }

        elif resource.resource_type == "ebs:volume":
            provisioned = resource.attributes.get("iops") or 3000
            resource.metrics = {"volume_iops_observed": float(provisioned) * 0.12}

        elif resource.resource_type == "ec2:nat-gateway":
            # The eu-west-1 gateway barely passes traffic; the us-east-1 one is load bearing.
            idle = resource.region != DEMO_REGIONS[0]
            resource.metrics = {"nat_bytes_per_day": 250_000.0 if idle else 90.0 * gb}

        elif resource.resource_type.startswith(("elbv2:", "elb:")):
            busy = "legacy" not in name
            resource.metrics = {
                "requests_per_day": 4_200_000.0 if busy else 3.0,
                "healthy_hosts": 4.0 if busy else 0.0,
            }

        elif resource.resource_type == "rds:db-instance":
            idle = "idle" in name or "reporting" in name
            resource.metrics = {
                "db_connections_max": 0.0 if idle else 84.0,
                "db_connections_avg": 0.0 if idle else 42.0,
                "cpu_avg": 1.1 if idle else 38.0,
            }

        elif resource.resource_type == "dynamodb:table":
            resource.metrics = {
                "read_utilization_percent": 6.0,
                "write_utilization_percent": 4.0,
            }

        elif resource.resource_type == "lambda:function":
            resource.metrics = {
                "invocations_per_month": 240_000.0 if "checkout" in name else 900.0,
                "duration_avg_ms": 180.0,
            }

        elif resource.resource_type == "s3:bucket":
            size_gb = {0: 480, 1: 2_400, 2: 8_600}[variant]
            resource.metrics = {
                "bucket_size_bytes": float(size_gb) * gb,
                "object_count": float(size_gb) * 320,
            }

        elif resource.resource_type == "logs:log-group":
            resource.metrics = {"stored_bytes": float({0: 45, 1: 180, 2: 720}[variant]) * gb}


def demo_cost_snapshot(lookback_days: int) -> CostSnapshot:
    """A synthetic bill shaped like a mid-size production account."""
    end = date.today()
    start = end - timedelta(days=lookback_days)

    service_totals = {
        "Amazon Elastic Compute Cloud - Compute": 8420.0,
        "Amazon Relational Database Service": 3180.0,
        "Amazon Elastic Container Service for Kubernetes": 1460.0,
        "EC2 - Other": 2740.0,
        "Amazon Simple Storage Service": 1290.0,
        "Amazon CloudWatch": 640.0,
        "AWS Lambda": 310.0,
        "Amazon DynamoDB": 470.0,
        "Amazon Virtual Private Cloud": 890.0,
        "AWS Data Transfer": 720.0,
        "Amazon Route 53": 90.0,
        "AWS Key Management Service": 45.0,
    }
    total = sum(service_totals.values())
    region_totals = {
        "us-east-1": round(total * 0.72, 2),
        "eu-west-1": round(total * 0.23, 2),
        "global": round(total * 0.05, 2),
    }
    usage_type_totals = {
        "BoxUsage:m5.xlarge": 2100.0,
        "BoxUsage:c5.4xlarge": 1740.0,
        "EBS:VolumeUsage.gp2": 1180.0,
        "NatGateway-Bytes": 610.0,
        "NatGateway-Hours": 280.0,
        "LoadBalancerUsage": 340.0,
        "TimedStorage-ByteHrs": 890.0,
        "DataTransfer-Out-Bytes": 720.0,
        "RDS:GP2-Storage": 520.0,
        "CW:Requests": 240.0,
    }

    # A weekly rhythm plus mild growth reads like a real daily cost series.
    daily_totals: dict[str, float] = {}
    records: list[CostRecord] = []
    per_day = total / max(lookback_days, 1)
    for offset in range(lookback_days):
        day = start + timedelta(days=offset)
        weekend = 0.82 if day.weekday() >= 5 else 1.0
        drift = 1 + (offset / max(lookback_days, 1)) * 0.06
        amount = round(per_day * weekend * drift, 2)
        daily_totals[day.isoformat()] = amount
        records.append(
            CostRecord(
                period_start=day,
                period_end=day + timedelta(days=1),
                granularity="DAILY",
                amount=amount,
            )
        )

    for service, amount in service_totals.items():
        records.append(
            CostRecord(
                period_start=start,
                period_end=end,
                granularity="MONTHLY",
                amount=amount,
                dimensions={"SERVICE": service},
            )
        )

    observed_total = round(sum(daily_totals.values()), 2)
    return CostSnapshot(
        period_start=start,
        period_end=end,
        records=records,
        total_cost=observed_total,
        month_to_date_cost=round(observed_total * 0.62, 2),
        previous_period_cost=round(observed_total * 0.94, 2),
        forecast_next_month=round(observed_total * 1.05, 2),
        forecast_lower=round(observed_total * 0.98, 2),
        forecast_upper=round(observed_total * 1.14, 2),
        service_totals=service_totals,
        region_totals=region_totals,
        usage_type_totals=usage_type_totals,
        daily_totals=daily_totals,
        resource_level_available=False,
        commitments=CommitmentSummary(
            savings_plans_coverage_percent=38.0,
            savings_plans_utilization_percent=96.5,
            reservation_coverage_percent=12.0,
            reservation_utilization_percent=88.0,
            savings_plans_recommendation={
                "HourlyCommitmentToPurchase": "3.20",
                "EstimatedMonthlySavingsAmount": "740.00",
                "EstimatedSavingsPercentage": "18.4",
            },
        ),
    )


def demo_summary(scan: Scan) -> dict[str, Any]:  # pragma: no cover - debugging aid
    return {
        "resources": len(scan.resources),
        "findings": len(scan.findings),
        "savings": scan.tco.identified_monthly_savings if scan.tco else 0.0,
    }
