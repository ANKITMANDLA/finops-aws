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

        efs = boto3.client("efs", region_name=TEST_REGION)
        file_system = efs.create_file_system(
            CreationToken="shared",
            ThroughputMode="provisioned",
            ProvisionedThroughputInMibps=64.0,
            Tags=[{"Key": "Name", "Value": "shared-data"}],
        )
        efs.put_lifecycle_configuration(
            FileSystemId=file_system["FileSystemId"],
            LifecyclePolicies=[
                {"TransitionToIA": "AFTER_30_DAYS"},
                {"TransitionToPrimaryStorageClass": "AFTER_1_ACCESS"},
            ],
        )

        # A transit gateway with one attachment, an interface endpoint alongside a free
        # gateway endpoint, and a VPN connection.
        gateway = ec2.create_transit_gateway(Description="hub")["TransitGateway"]
        attachment = ec2.create_transit_gateway_vpc_attachment(
            TransitGatewayId=gateway["TransitGatewayId"],
            VpcId=vpc["VpcId"],
            SubnetIds=[subnet_a["SubnetId"]],
        )["TransitGatewayVpcAttachment"]
        interface_endpoint = ec2.create_vpc_endpoint(
            VpcId=vpc["VpcId"],
            ServiceName=f"com.amazonaws.{TEST_REGION}.secretsmanager",
            VpcEndpointType="Interface",
            SubnetIds=[subnet_a["SubnetId"], subnet_b["SubnetId"]],
        )["VpcEndpoint"]
        ec2.create_vpc_endpoint(
            VpcId=vpc["VpcId"],
            ServiceName=f"com.amazonaws.{TEST_REGION}.s3",
            VpcEndpointType="Gateway",
        )
        customer_gateway = ec2.create_customer_gateway(
            Type="ipsec.1", PublicIp="203.0.113.5", BgpAsn=65000
        )["CustomerGateway"]
        vpn_gateway = ec2.create_vpn_gateway(Type="ipsec.1")["VpnGateway"]
        ec2.create_vpn_connection(
            Type="ipsec.1",
            CustomerGatewayId=customer_gateway["CustomerGatewayId"],
            VpnGatewayId=vpn_gateway["VpnGatewayId"],
        )

        kms = boto3.client("kms", region_name=TEST_REGION)
        customer_key = kms.create_key(Description="app data")["KeyMetadata"]
        kms.create_alias(AliasName="alias/app-data", TargetKeyId=customer_key["KeyId"])

        secrets = boto3.client("secretsmanager", region_name=TEST_REGION)
        secrets.create_secret(Name="prod/db/password", SecretString="hunter2")

        acm = boto3.client("acm", region_name=TEST_REGION)
        certificate = acm.request_certificate(DomainName="test.example.com")

        sns = boto3.client("sns", region_name=TEST_REGION)
        topic = sns.create_topic(Name="alerts")

        sqs = boto3.client("sqs", region_name=TEST_REGION)
        sqs.create_queue(QueueName="jobs")

        ecr = boto3.client("ecr", region_name=TEST_REGION)
        ecr.create_repository(repositoryName="team/api")

        cloudwatch = boto3.client("cloudwatch", region_name=TEST_REGION)
        cloudwatch.put_metric_alarm(
            AlarmName="cpu-high",
            MetricName="CPUUtilization",
            Namespace="AWS/EC2",
            Statistic="Average",
            Period=300,
            EvaluationPeriods=1,
            Threshold=80.0,
            ComparisonOperator="GreaterThanThreshold",
        )

        yield {
            "instance_id": instance_id,
            "volume_id": volume["VolumeId"],
            "file_system_id": file_system["FileSystemId"],
            "transit_gateway_id": gateway["TransitGatewayId"],
            "attachment_id": attachment["TransitGatewayAttachmentId"],
            "endpoint_id": interface_endpoint["VpcEndpointId"],
            "key_id": customer_key["KeyId"],
            "certificate_arn": certificate["CertificateArn"],
            "topic_arn": topic["TopicArn"],
        }


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
        "efs",
        "s3",
        "lambda",
        "dynamodb",
        "logs",
        "tgw",
        "vpce",
        "vpn",
        "kms",
        "secrets",
        "acm",
        "sns",
        "sqs",
        "ecr",
        "alarms",
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

    file_system = grouped["efs:file-system"][0]
    assert file_system.resource_id == seeded_account["file_system_id"]
    assert file_system.name == "shared-data"
    assert file_system.attributes["throughput_mode"] == "provisioned"
    assert file_system.attributes["provisioned_throughput_mibps"] == 64.0
    assert file_system.attributes["transition_to_ia"] == "AFTER_30_DAYS"
    assert file_system.attributes["transition_to_primary_storage_class"] == "AFTER_1_ACCESS"
    assert file_system.attributes["one_zone"] is False
    assert file_system.attributes["mount_target_count"] == 0

    # Every resource must carry the identifying fields the rest of the pipeline needs.
    for resource in resources:
        assert resource.arn and resource.resource_id and resource.region
        assert resource.account_id == "123456789012"


def test_transit_gateway_attachments_record_who_pays(seeded_account, collection_context):
    resources = collect_inventory(collection_context, only=["tgw"], regions=[TEST_REGION])
    grouped = _by_type(resources)

    gateway = grouped["ec2:transit-gateway"][0]
    assert gateway.resource_id == seeded_account["transit_gateway_id"]
    assert gateway.attributes["owned_by_this_account"] is True

    attachment = grouped["ec2:transit-gateway-attachment"][0]
    assert attachment.resource_id == seeded_account["attachment_id"]
    assert attachment.attributes["attachment_kind"] == "vpc"
    assert attachment.attributes["transit_gateway_id"] == seeded_account["transit_gateway_id"]
    assert attachment.attributes["owned_by_this_account"] is True


def test_a_shared_transit_gateway_is_not_billed_to_this_account(
    seeded_account, collection_context, monkeypatch
):
    """A gateway shared in through RAM looks local but belongs to someone else."""
    from finops.aws.collectors.network import TransitGatewayCollector

    original = collection_context.client

    def patched(service: str, region: str):
        client = original(service, region)
        if service != "ec2":
            return client
        describe = client.describe_transit_gateways

        def owned_elsewhere(**kwargs):
            response = describe(**kwargs)
            for gateway in response.get("TransitGateways", []):
                gateway["OwnerId"] = "999988887777"
            return response

        client.describe_transit_gateways = owned_elsewhere
        return client

    monkeypatch.setattr(collection_context, "client", patched)
    resources = TransitGatewayCollector().collect(collection_context, TEST_REGION)

    gateway = next(r for r in resources if r.resource_type == "ec2:transit-gateway")
    assert gateway.attributes["owned_by_this_account"] is False
    assert gateway.account_id == "999988887777"


def test_only_billable_vpc_endpoints_are_marked_as_such(seeded_account, collection_context):
    resources = collect_inventory(collection_context, only=["vpce"], regions=[TEST_REGION])
    interface = next(r for r in resources if r.resource_id == seeded_account["endpoint_id"])
    gateway = next(r for r in resources if r.attributes["endpoint_type"] == "Gateway")

    assert interface.attributes["billable"] is True
    # Charged per availability zone, and it was placed in two subnets.
    assert interface.attributes["network_interface_count"] == 2
    assert interface.attributes["service_name"].endswith("secretsmanager")
    assert gateway.attributes["billable"] is False


def test_vpn_connections_report_tunnel_health(seeded_account, collection_context):
    resources = collect_inventory(collection_context, only=["vpn"], regions=[TEST_REGION])
    connection = next(r for r in resources if r.resource_type == "ec2:vpn-connection")
    assert connection.attributes["customer_gateway_id"].startswith("cgw-")
    assert connection.attributes["tunnels_up"] == connection.attributes["tunnel_status"].count("UP")


def test_only_customer_managed_kms_keys_are_collected(seeded_account, collection_context):
    """AWS managed keys are free, so carrying them would inflate the estate for nothing."""
    resources = collect_inventory(collection_context, only=["kms"], regions=[TEST_REGION])
    assert [r.resource_id for r in resources] == [seeded_account["key_id"]]
    key = resources[0]
    assert key.attributes["key_manager"] == "CUSTOMER"
    assert key.name == "alias/app-data"


def test_secrets_and_certificates_are_collected(seeded_account, collection_context):
    resources = collect_inventory(
        collection_context, only=["secrets", "acm"], regions=[TEST_REGION]
    )
    grouped = _by_type(resources)

    secret = grouped["secretsmanager:secret"][0]
    assert secret.resource_id == "prod/db/password"
    assert secret.state == "active"

    certificate = grouped["acm:certificate"][0]
    assert certificate.arn == seeded_account["certificate_arn"]
    assert certificate.attributes["domain_name"] == "test.example.com"
    assert certificate.attributes["in_use"] is False


def test_messaging_and_registry_resources_are_collected(seeded_account, collection_context):
    resources = collect_inventory(
        collection_context, only=["sns", "sqs", "ecr", "alarms"], regions=[TEST_REGION]
    )
    grouped = _by_type(resources)

    topic = grouped["sns:topic"][0]
    assert topic.arn == seeded_account["topic_arn"]
    assert topic.attributes["subscription_count"] == 0

    queue = grouped["sqs:queue"][0]
    assert queue.resource_id == "jobs"
    assert queue.attributes["fifo"] is False

    repository = grouped["ecr:repository"][0]
    assert repository.resource_id == "team/api"
    assert repository.attributes["has_lifecycle_policy"] is False
    # An empty registry is zero bytes, which is different from an unreadable one.
    assert repository.attributes["size_gb"] == 0.0
    assert repository.attributes["image_count"] == 0

    alarm = grouped["cloudwatch:alarm"][0]
    assert alarm.resource_id == "cpu-high"
    assert alarm.attributes["alarm_kind"] == "standard"


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
