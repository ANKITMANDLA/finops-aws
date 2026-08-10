from __future__ import annotations

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from finops.aws.collectors import REGISTRY, collect_inventory
from finops.aws.collectors.base import Collector, build_collectors, tags_to_dict
from finops.aws.errors import NoteCollector, classify_error_code, graceful

TEST_REGION = "us-east-1"


def _by_type(resources):
    grouped: dict[str, list] = {}
    for resource in resources:
        grouped.setdefault(resource.resource_type, []).append(resource)
    return grouped


@pytest.fixture
def seeded_account():
    """Create a small but representative account inside moto."""
    with mock_aws():
        ec2 = boto3.client("ec2", region_name=TEST_REGION)
        vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]
        subnet_a = ec2.create_subnet(
            VpcId=vpc["VpcId"], CidrBlock="10.0.1.0/24", AvailabilityZone="us-east-1a"
        )["Subnet"]
        subnet_b = ec2.create_subnet(
            VpcId=vpc["VpcId"], CidrBlock="10.0.2.0/24", AvailabilityZone="us-east-1b"
        )["Subnet"]

        image_id = ec2.describe_images()["Images"][0]["ImageId"]
        instances = ec2.run_instances(
            ImageId=image_id,
            MinCount=1,
            MaxCount=1,
            InstanceType="t3.large",
            SubnetId=subnet_a["SubnetId"],
            TagSpecifications=[
                {
                    "ResourceType": "instance",
                    "Tags": [{"Key": "Name", "Value": "web-1"}, {"Key": "env", "Value": "prod"}],
                }
            ],
        )["Instances"]
        instance_id = instances[0]["InstanceId"]

        # An unattached volume plus a snapshot of it.
        volume = ec2.create_volume(
            AvailabilityZone="us-east-1a",
            Size=100,
            VolumeType="gp2",
            TagSpecifications=[
                {"ResourceType": "volume", "Tags": [{"Key": "Name", "Value": "orphan-data"}]}
            ],
        )
        ec2.create_snapshot(VolumeId=volume["VolumeId"], Description="nightly")

        # An unassociated Elastic IP and a NAT Gateway.
        address = ec2.allocate_address(Domain="vpc")
        ec2.create_nat_gateway(SubnetId=subnet_a["SubnetId"], AllocationId=address["AllocationId"])
        ec2.allocate_address(Domain="vpc")

        elbv2 = boto3.client("elbv2", region_name=TEST_REGION)
        elbv2.create_load_balancer(
            Name="public-alb",
            Subnets=[subnet_a["SubnetId"], subnet_b["SubnetId"]],
            Type="application",
            Tags=[{"Key": "env", "Value": "prod"}],
        )

        s3 = boto3.client("s3", region_name=TEST_REGION)
        s3.create_bucket(Bucket="finops-test-bucket")

        ddb = boto3.client("dynamodb", region_name=TEST_REGION)
        ddb.create_table(
            TableName="orders",
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
            ProvisionedThroughput={"ReadCapacityUnits": 25, "WriteCapacityUnits": 25},
        )

        logs = boto3.client("logs", region_name=TEST_REGION)
        logs.create_log_group(logGroupName="/aws/lambda/never-expires")

        yield {"instance_id": instance_id, "volume_id": volume["VolumeId"]}


def test_registry_covers_the_expected_services():
    assert {
        "ec2",
        "autoscaling",
        "ebs",
        "ebs-snapshot",
        "ami",
        "eip",
        "natgw",
        "elbv2",
        "elb-classic",
        "eks",
        "rds",
        "rds-cluster",
        "rds-snapshot",
        "s3",
        "lambda",
        "dynamodb",
        "logs",
    } <= set(REGISTRY)


def test_collect_inventory_discovers_seeded_resources(seeded_account, collection_context):
    resources = collect_inventory(collection_context, regions=[TEST_REGION])
    grouped = _by_type(resources)

    instance = grouped["ec2:instance"][0]
    assert instance.attributes["instance_type"] == "t3.large"
    assert instance.attributes["lifecycle"] == "on-demand"
    assert instance.name == "web-1"
    assert instance.tags["env"] == "prod"
    assert instance.availability_zone == "us-east-1a"

    volume = next(v for v in grouped["ebs:volume"] if v.state == "available")
    assert volume.attributes["size_gb"] == 100
    assert volume.attributes["volume_type"] == "gp2"
    assert volume.attributes["attached_instance_id"] is None

    snapshot = next(
        s
        for s in grouped["ebs:snapshot"]
        if s.attributes["volume_id"] == seeded_account["volume_id"]
    )
    assert snapshot.attributes["volume_size_gb"] == 100

    elastic_ips = grouped["ec2:elastic-ip"]
    assert any(ip.state == "unassociated" for ip in elastic_ips)
    assert grouped["ec2:nat-gateway"][0].attributes["connectivity_type"] == "public"

    alb = grouped["elbv2:application"][0]
    assert alb.attributes["lb_type"] == "application"
    assert alb.attributes["target_group_count"] == 0

    bucket = grouped["s3:bucket"][0]
    assert bucket.region == TEST_REGION
    assert bucket.attributes["has_lifecycle"] is False

    table = grouped["dynamodb:table"][0]
    assert table.attributes["billing_mode"] == "PROVISIONED"
    assert table.attributes["read_capacity_units"] == 25

    log_group = grouped["logs:log-group"][0]
    assert log_group.attributes["never_expires"] is True

    # Every resource must carry the identifying fields the rest of the pipeline needs.
    for resource in resources:
        assert resource.arn and resource.resource_id and resource.region
        assert resource.account_id == "123456789012"


def test_collect_inventory_honours_the_only_filter(seeded_account, collection_context):
    resources = collect_inventory(collection_context, only=["ec2"], regions=[TEST_REGION])
    assert {r.resource_type for r in resources} == {"ec2:instance"}


def test_global_collector_skips_regions_outside_the_scan(seeded_account, collection_context):
    resources = collect_inventory(collection_context, only=["s3"], regions=["eu-west-1"])
    assert resources == []


def test_build_collectors_rejects_unknown_keys():
    with pytest.raises(ValueError, match="Unknown collector"):
        build_collectors(["not-a-service"])


def test_a_failing_collector_is_isolated_and_recorded(
    seeded_account, collection_context, monkeypatch
):
    class ExplodingCollector(Collector):
        key = "exploding"
        service = "Test"

        def collect(self, ctx, region):
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "not allowed"}}, "DescribeThings"
            )

    monkeypatch.setitem(REGISTRY, "exploding", ExplodingCollector)

    resources = collect_inventory(
        collection_context, only=["ec2", "exploding"], regions=[TEST_REGION]
    )

    # The healthy collector still returned data.
    assert any(r.resource_type == "ec2:instance" for r in resources)
    note = next(n for n in collection_context.notes.notes if n.capability == "exploding")
    assert note.status == "denied"
    assert "iam/finops-readonly-policy.json" in (note.remedy or "")


def test_notes_deduplicate_the_same_failure_across_regions():
    notes = NoteCollector()
    for region in ("us-east-1", "us-east-1", "us-west-2"):
        with graceful(notes, "compute-optimizer", region=region):
            raise ClientError(
                {"Error": {"Code": "OptInRequired", "Message": "enroll first"}}, "GetRecs"
            )
    assert len(notes.notes) == 2
    assert {n.status for n in notes.notes} == {"not_enrolled"}
    assert notes.has_problem("compute-optimizer")


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("AccessDeniedException", "denied"),
        ("OptInRequired", "not_enrolled"),
        ("DataUnavailableException", "unavailable"),
        ("ThrottlingException", "error"),
    ],
)
def test_error_codes_map_to_capability_status(code, expected):
    assert classify_error_code(code) == expected


def test_tags_to_dict_handles_missing_and_odd_shapes():
    assert tags_to_dict(None) == {}
    assert tags_to_dict([{"Key": "a", "Value": "1"}, {"Key": "b"}]) == {"a": "1", "b": ""}
