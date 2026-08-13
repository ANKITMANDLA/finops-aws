from __future__ import annotations

from datetime import date

import pytest
from tests.factories import make_resource
from tests.fakes import FakeAwsContext, FakePricingClient, price_list_entry

from finops.attribution import attribute_costs
from finops.aws.costs import CostSnapshot
from finops.aws.errors import NoteCollector
from finops.aws.pricing import (
    HOURS_PER_MONTH,
    PricingClient,
    _first_paid_rate,
    efs_billable_throughput_mibps,
)

# Prices as AWS publishes them, keyed the way FakePricingClient looks them up.
LIST_PRICES = {
    "t3.large": "0.0832",
    "m5.large": "0.096",
    "gp2": ("0.10", "USW2-EBS:VolumeUsage.gp2"),
    "gp3": ("0.08", "USW2-EBS:VolumeUsage.gp3"),
    "io1": ("0.125", "USW2-EBS:VolumeUsage.piops"),
    "Storage Snapshot": ("0.05", "USW2-EBS:SnapshotUsage"),
    "NAT Gateway": ("0.045", "USW2-RegionalNatGateway-Hours"),
    "Load Balancer-Application": ("0.0225", "USW2-LoadBalancerUsage"),
    "VPCPublicIPv4Address": ("0.005", "USW2-PublicIPv4:InUseAddress"),
    "Compute": ("0.10", "USW2-AmazonEKS-Hours:perCluster"),
    "Standard": ("0.023", "USW2-TimedStorage-ByteHrs"),
    "AWS-Lambda-Duration": ("0.0000166667", "USW2-Lambda-GB-Second"),
    "AWS-Lambda-Requests": ("0.0000002", "USW2-Request"),
    "DDB-ReadUnits": ("0.00013", "USW2-ReadCapacityUnit-Hrs"),
    "DDB-WriteUnits": ("0.00065", "USW2-WriteCapacityUnit-Hrs"),
    "Amazon DynamoDB - Indexed DataStore": ("0.25", "USW2-TimedStorage-ByteHrs"),
}


def build_pricing(prices=None, *, fail=False, tmp_path=None, settings=None):
    api = FakePricingClient(prices, fail=fail)
    client = PricingClient(
        aws=FakeAwsContext(api, settings),
        notes=NoteCollector(),
        cache_path=(tmp_path / "pricing.json") if tmp_path else None,
    )
    return client, api


# ---------------------------------------------------------------- price list parsing


def rate(*args, **kwargs) -> float | None:
    """The amount from a lookup, dropping the unit AWS quoted it in."""
    quoted = _first_paid_rate(*args, **kwargs)
    return quoted[0] if quoted else None


def test_the_first_paid_tier_is_the_rate_we_quote():
    assert rate([price_list_entry("0.0832")]) == 0.0832
    # A free allowance published as tier one must not read as a price of zero.
    tiered = price_list_entry("", tiers={"0": "0.0000", "18600": "0.00013"})
    assert rate([tiered]) == 0.00013
    # Volume discounts are published lowest tier first; that is the rate to quote.
    discounted = price_list_entry("", tiers={"51200": "0.022", "0": "0.023"})
    assert rate([discounted]) == 0.023


def test_the_unit_a_rate_was_quoted_in_comes_back_with_it():
    quoted = _first_paid_rate([price_list_entry("40.96", unit="GiBps-mo")])

    assert quoted == (40.96, "GiBps-mo")


def test_nothing_to_price_is_reported_as_no_price():
    assert rate([]) is None
    assert rate(["not json"]) is None
    assert rate([price_list_entry("0.0000")]) is None


def test_a_usage_type_matches_through_any_region_prefix():
    regional = price_list_entry("0.0225", usage_type="USW2-LoadBalancerUsage")
    unprefixed = price_list_entry("0.0225", usage_type="LoadBalancerUsage")

    assert rate([regional], "LoadBalancerUsage") == 0.0225
    assert rate([unprefixed], "LoadBalancerUsage") == 0.0225


def test_a_neighbouring_usage_type_is_not_mistaken_for_the_one_we_asked_for():
    # Traffic-shaped and Outposts load balancer hours sit in the same product family at a
    # different price, so matching on the tail of the usage type has to reject them.
    traffic_shaped = price_list_entry("0.0063", usage_type="USW2-TS-LoadBalancerUsage")
    infrequent_access_logs = price_list_entry("0.018", usage_type="USW2-TimedStorage-IA-ByteHrs")

    assert rate([traffic_shaped], "LoadBalancerUsage") is None
    assert rate([infrequent_access_logs], "TimedStorage-ByteHrs") is None


def test_the_nat_gateway_rate_is_found_under_either_published_name():
    for usage in ("USW2-NatGateway-Hours", "USW2-RegionalNatGateway-Hours"):
        entry = price_list_entry("0.045", usage_type=usage)
        assert rate([entry], "(Regional)?NatGateway-Hours") == 0.045


# ------------------------------------------------------------------------- lookups


def test_instance_price_is_converted_to_a_monthly_figure(tmp_path, settings):
    pricing, _ = build_pricing(LIST_PRICES, tmp_path=tmp_path, settings=settings)
    assert pricing.ec2_instance_monthly("us-east-1", "m5.large") == pytest.approx(
        0.096 * HOURS_PER_MONTH
    )


def test_repeated_lookups_hit_the_cache_not_the_api(tmp_path, settings):
    pricing, api = build_pricing(LIST_PRICES, tmp_path=tmp_path, settings=settings)
    for _ in range(4):
        pricing.ec2_instance_hourly("us-east-1", "m5.large")
    assert api.call_count == 1


def test_cache_persists_across_client_instances(tmp_path, settings):
    pricing, api = build_pricing(LIST_PRICES, tmp_path=tmp_path, settings=settings)
    pricing.ec2_instance_hourly("us-east-1", "m5.large")
    pricing.save_cache()

    reloaded, fresh_api = build_pricing({}, tmp_path=tmp_path, settings=settings)
    price = reloaded.ec2_instance_hourly("us-east-1", "m5.large")
    assert price is not None and price.amount == 0.096
    assert fresh_api.call_count == 0


def test_a_denied_pricing_api_yields_no_price_at_all(tmp_path, settings):
    pricing, api = build_pricing(fail=True, tmp_path=tmp_path, settings=settings)

    # Nothing is invented: every charge is unknown, including the ones whose rates barely
    # vary between regions.
    assert pricing.ec2_instance_hourly("us-east-1", "m5.large") is None
    assert pricing.ebs_gb_month("us-east-1", "gp3") is None
    assert pricing.nat_gateway_hourly("us-east-1") is None
    assert pricing.eks_cluster_hourly("us-east-1") is None

    # One failure disables further calls for the rest of the scan.
    assert api.call_count == 1
    assert not pricing.api_available
    assert pricing.notes.has_problem("pricing:GetProducts")


def test_filters_that_match_nothing_are_recorded_for_diagnosis(tmp_path, settings):
    pricing, _ = build_pricing({}, tmp_path=tmp_path, settings=settings)

    assert pricing.snapshot_gb_month("us-east-1") is None
    assert pricing.api_available
    assert pricing.unresolved == ["AmazonEC2 EBS:SnapshotUsage"]


def test_gp3_volume_cost_only_bills_iops_above_the_free_allowance(tmp_path, settings):
    prices = LIST_PRICES | {
        "gp3": ("0.08", "USW2-EBS:VolumeUsage.gp3"),
    }
    pricing, _ = build_pricing(prices, tmp_path=tmp_path, settings=settings)

    baseline = pricing.ebs_volume_monthly("us-east-1", "gp3", 100, iops=3000, throughput_mibps=125)
    assert baseline == pytest.approx(100 * 0.08)


def test_volume_iops_and_throughput_are_priced_from_their_own_lookups(tmp_path, settings):
    api = FakePricingClient(LIST_PRICES)
    # The three gp3 charges share a filter key, so answer each by product family.
    charges = {
        "Storage": ("0.08", "USW2-EBS:VolumeUsage.gp3", "GB-Mo"),
        "System Operation": ("0.005", "USW2-EBS:VolumeP-IOPS.gp3", "IOPS-Mo"),
        # AWS quotes gp3 throughput per GiBps even though it advertises it per MiBps.
        "Provisioned Throughput": ("40.96", "USW2-EBS:VolumeP-Throughput.gp3", "GiBps-mo"),
    }

    def get_products(**kwargs):
        api.call_count += 1
        filters = {f["Field"]: f["Value"] for f in kwargs["Filters"]}
        price, usage, unit = charges[filters["productFamily"]]
        return {"PriceList": [price_list_entry(price, unit, usage_type=usage)]}

    api.get_products = get_products
    pricing = PricingClient(
        aws=FakeAwsContext(api, settings), notes=NoteCollector(), cache_path=tmp_path / "p.json"
    )

    cost = pricing.ebs_volume_monthly("us-east-1", "gp3", 100, iops=6000, throughput_mibps=250)

    assert cost == pytest.approx(100 * 0.08 + 3000 * 0.005 + 125 * 0.04)


def test_a_charge_quoted_per_gibps_is_converted_to_the_rate_aws_advertises(tmp_path, settings):
    # $40.96 per GiBps-month is how AWS publishes "$0.04 per provisioned MiBps-month".
    prices = {"gp3": ("40.96", "USW2-EBS:VolumeP-Throughput.gp3", "GiBps-mo")}
    pricing, _ = build_pricing(prices, tmp_path=tmp_path, settings=settings)

    price = pricing.ebs_throughput_month("us-east-1", "gp3")

    assert price is not None
    assert price.amount == pytest.approx(0.04)


# EFS publishes each storage tier as its own class under one product family.
EFS_PRICES = {
    "General Purpose": ("0.30", "USW2-TimedStorage-ByteHrs", "GB-Mo"),
    "Infrequent Access": ("0.025", "USW2-IATimedStorage-ByteHrs", "GB-Mo"),
    "Infrequent Access-ET": ("0.016", "USW2-IATimedStorage-ET-ByteHrs", "GB-Mo"),
    "Archive": ("0.008", "USW2-ArchiveTimedStorage-ByteHrs", "GB-Mo"),
    "Provisioned Throughput": ("6.00", "USW2-ProvisionedTP-MiBpsHrs", "MiBps-Mo"),
}


def test_efs_storage_is_priced_at_the_rate_of_the_tier_each_byte_sits_in(tmp_path, settings):
    pricing, _ = build_pricing(EFS_PRICES, tmp_path=tmp_path, settings=settings)

    cost = pricing.efs_file_system_monthly(
        "us-west-2", standard_gb=100, ia_gb=400, archive_gb=1_000
    )

    assert cost == pytest.approx(100 * 0.30 + 400 * 0.025 + 1_000 * 0.008)


def test_efs_throughput_is_only_billed_above_what_stored_data_includes(tmp_path, settings):
    pricing, _ = build_pricing(EFS_PRICES, tmp_path=tmp_path, settings=settings)

    # 400 GB in Standard earns 20 MiB/s of baseline, so only 80 of the 100 are charged.
    cost = pricing.efs_file_system_monthly(
        "us-west-2",
        standard_gb=400,
        throughput_mode="provisioned",
        provisioned_mibps=100,
    )

    assert cost == pytest.approx(400 * 0.30 + 80 * 6.00)


def test_throughput_provisioned_below_the_baseline_costs_nothing_extra():
    # 200 GB in Standard already carries 10 MiB/s, so provisioning 5 adds no charge.
    assert efs_billable_throughput_mibps(200, "provisioned", 5) == 0.0
    assert efs_billable_throughput_mibps(200, "provisioned", 30) == pytest.approx(20.0)
    # Bursting and Elastic file systems have no throughput charge at all.
    assert efs_billable_throughput_mibps(200, "bursting", None) == 0.0
    assert efs_billable_throughput_mibps(200, "elastic", None) == 0.0


def test_elastic_file_systems_are_priced_on_the_cold_rate_published_for_them(tmp_path, settings):
    pricing, _ = build_pricing(EFS_PRICES, tmp_path=tmp_path, settings=settings)

    elastic = pricing.efs_file_system_monthly(
        "us-west-2", standard_gb=0, ia_gb=1_000, throughput_mode="elastic"
    )
    bursting = pricing.efs_file_system_monthly(
        "us-west-2", standard_gb=0, ia_gb=1_000, throughput_mode="bursting"
    )

    assert elastic == pytest.approx(1_000 * 0.016)
    assert bursting == pytest.approx(1_000 * 0.025)


def test_multi_az_rds_storage_is_not_doubled_on_top_of_its_own_rate(tmp_path, settings):
    pricing, api = build_pricing(
        {"General Purpose-GP3": ("0.23", "USW2-RDS:Multi-AZ-GP3-Storage")},
        tmp_path=tmp_path,
        settings=settings,
    )

    price = pricing.rds_storage_gb_month("us-east-1", "gp3", multi_az=True)

    assert price is not None and price.amount == 0.23
    assert api.requests[0]["volumeType"] == "General Purpose-GP3"


# ------------------------------------------------------------------ attribution


def empty_snapshot(**kwargs):
    return CostSnapshot(period_start=date(2026, 7, 1), period_end=date(2026, 7, 31), **kwargs)


def test_billed_cost_beats_the_list_price_estimate(tmp_path, settings):
    pricing, _ = build_pricing(LIST_PRICES, tmp_path=tmp_path, settings=settings)
    instance = make_resource("i-0123456789abcdef0", attributes={"instance_type": "t3.large"})
    snapshot = empty_snapshot(resource_costs={"i-0123456789abcdef0": 44.0})

    attribute_costs([instance], snapshot, pricing)

    assert instance.monthly_cost == 44.0
    assert instance.cost_basis == "actual_resource_level"


def test_list_price_is_used_when_no_billed_cost_exists(tmp_path, settings):
    pricing, _ = build_pricing(LIST_PRICES, tmp_path=tmp_path, settings=settings)
    instance = make_resource(
        "i-abc",
        attributes={"instance_type": "t3.large", "platform_details": "Linux/UNIX"},
    )

    attribute_costs([instance], empty_snapshot(), pricing)

    assert instance.monthly_cost == pytest.approx(0.0832 * HOURS_PER_MONTH, abs=0.01)
    assert instance.cost_basis == "list_price_estimate"


def test_stopped_instances_and_amis_cost_nothing_of_their_own(tmp_path, settings):
    pricing, _ = build_pricing(LIST_PRICES, tmp_path=tmp_path, settings=settings)
    stopped = make_resource("i-stopped", state="stopped", attributes={"instance_type": "t3.large"})
    ami = make_resource(
        "ami-1", resource_type="ec2:image", service="AMI", attributes={"backing_size_gb": 8}
    )

    attribute_costs([stopped, ami], empty_snapshot(), pricing)

    # Compute stops billing; the attached volume is charged against the volume itself.
    assert stopped.monthly_cost == 0.0
    # The AMI's snapshots are inventoried separately, so pricing it here would double count.
    assert ami.monthly_cost == 0.0


def test_spot_instances_are_left_unpriced_rather_than_overstated(tmp_path, settings):
    pricing, _ = build_pricing(LIST_PRICES, tmp_path=tmp_path, settings=settings)
    spot = make_resource("i-spot", attributes={"instance_type": "t3.large", "lifecycle": "spot"})

    attribute_costs([spot], empty_snapshot(), pricing)

    assert spot.monthly_cost is None
    assert spot.cost_basis is None


def test_unattached_volume_is_priced_from_its_size_and_type(tmp_path, settings):
    pricing, _ = build_pricing(LIST_PRICES, tmp_path=tmp_path, settings=settings)
    volume = make_resource(
        "vol-1",
        resource_type="ebs:volume",
        service="EBS",
        state="available",
        attributes={"volume_type": "gp2", "size_gb": 200},
        monthly_cost=None,
    )

    attribute_costs([volume], empty_snapshot(), pricing)

    assert volume.monthly_cost == pytest.approx(200 * 0.10)
    assert volume.cost_basis == "list_price_estimate"


def test_an_idle_public_ip_is_priced_on_the_idle_rate(tmp_path, settings):
    pricing, api = build_pricing(
        {"VPCPublicIPv4Address": ("0.005", "USW2-PublicIPv4:IdleAddress")},
        tmp_path=tmp_path,
        settings=settings,
    )
    address = make_resource(
        "eipalloc-1",
        resource_type="ec2:elastic-ip",
        service="VPC",
        state="unassociated",
        attributes={"associated": False},
        monthly_cost=None,
    )

    attribute_costs([address], empty_snapshot(), pricing)

    assert address.monthly_cost == pytest.approx(0.005 * HOURS_PER_MONTH, abs=0.01)
    assert api.requests[0]["group"] == "VPCPublicIPv4Address"


def test_log_group_cost_comes_from_stored_bytes(tmp_path, settings):
    pricing, _ = build_pricing(
        {"Storage Snapshot": ("0.03", "USW2-TimedStorage-ByteHrs")},
        tmp_path=tmp_path,
        settings=settings,
    )
    log_group = make_resource(
        "/aws/lambda/api",
        resource_type="logs:log-group",
        service="CloudWatch Logs",
        attributes={"stored_gb": 500.0, "never_expires": True},
        monthly_cost=None,
    )

    attribute_costs([log_group], empty_snapshot(), pricing)

    assert log_group.monthly_cost == pytest.approx(500.0 * 0.03, abs=0.01)


def test_efs_cost_comes_from_the_tier_sizes_the_file_system_reports(tmp_path, settings):
    pricing, _ = build_pricing(EFS_PRICES, tmp_path=tmp_path, settings=settings)
    file_system = make_resource(
        "fs-1",
        resource_type="efs:file-system",
        service="EFS",
        state="available",
        attributes={
            "standard_gb": 200.0,
            "ia_gb": 800.0,
            "archive_gb": 0.0,
            "throughput_mode": "provisioned",
            "provisioned_throughput_mibps": 50.0,
        },
        monthly_cost=None,
    )

    attribute_costs([file_system], empty_snapshot(), pricing)

    # 200 GB of Standard carries 10 MiB/s, leaving 40 of the 50 provisioned to pay for.
    expected = 200 * 0.30 + 800 * 0.025 + 40 * 6.00
    assert file_system.monthly_cost == pytest.approx(expected, abs=0.01)
    assert file_system.cost_basis == "list_price_estimate"


def test_a_provisioned_dynamodb_table_is_priced_from_published_capacity_rates(tmp_path, settings):
    pricing, _ = build_pricing(LIST_PRICES, tmp_path=tmp_path, settings=settings)
    table = make_resource(
        "orders",
        resource_type="dynamodb:table",
        service="DynamoDB",
        attributes={
            "billing_mode": "PROVISIONED",
            "read_capacity_units": 10,
            "write_capacity_units": 5,
        },
        monthly_cost=None,
    )

    attribute_costs([table], empty_snapshot(), pricing)

    expected = (10 * 0.00013 + 5 * 0.00065) * HOURS_PER_MONTH
    assert table.monthly_cost == pytest.approx(expected, abs=0.01)


CONNECTIVITY_PRICES = {
    "TransitGatewayVPC": ("0.05", "USW2-TransitGateway-Hours"),
    "VpcEndpoint": ("0.01", "USW2-VpcEndpoint-Hours"),
    "Cloud Connectivity": ("0.05", r"USW2-VPN-Usage-Hours:ipsec.1"),
    "ClientVPNEndpoints": ("0.10", "USW2-ClientVPN-EndpointHours"),
    "Encryption Key": ("1.00", "us-west-2-KMS-Keys", "Keys"),
    "Secret": ("0.40", "USW2-AWSSecretsManager-Secrets", "Secrets"),
    "AWS Certificate Manager": ("400.00", "USW2-PaidPrivateCA", "CertificateAuthorities"),
    "EC2 Container Registry": ("0.10", "USW2-TimedStorage-ByteHrs", "GB-Mo"),
    "Alarm": ("0.10", "USW2-CW:AlarmMonitorUsage", "Alarms"),
    "API Request": ("0.0000005", "USW2-Requests-Tier1", "Requests"),
}


def test_a_transit_gateway_attachment_is_billed_to_whoever_created_it(tmp_path, settings):
    pricing, _ = build_pricing(CONNECTIVITY_PRICES, tmp_path=tmp_path, settings=settings)
    mine = make_resource(
        "tgw-attach-1",
        resource_type="ec2:transit-gateway-attachment",
        service="VPC",
        state="available",
        attributes={"attachment_kind": "vpc", "owned_by_this_account": True},
        monthly_cost=None,
    )
    someone_elses = make_resource(
        "tgw-attach-2",
        resource_type="ec2:transit-gateway-attachment",
        service="VPC",
        state="available",
        attributes={"attachment_kind": "vpc", "owned_by_this_account": False},
        monthly_cost=None,
    )
    gateway = make_resource(
        "tgw-1",
        resource_type="ec2:transit-gateway",
        service="VPC",
        state="available",
        attributes={"owned_by_this_account": True},
        monthly_cost=None,
    )

    attribute_costs([mine, someone_elses, gateway], empty_snapshot(), pricing)

    assert mine.monthly_cost == pytest.approx(0.05 * HOURS_PER_MONTH, abs=0.01)
    assert someone_elses.monthly_cost == 0.0
    # The gateway itself carries no charge; the attachments do.
    assert gateway.monthly_cost == 0.0


def test_an_interface_endpoint_is_charged_once_per_availability_zone(tmp_path, settings):
    pricing, _ = build_pricing(CONNECTIVITY_PRICES, tmp_path=tmp_path, settings=settings)
    interface = make_resource(
        "vpce-1",
        resource_type="ec2:vpc-endpoint",
        service="VPC",
        state="available",
        attributes={
            "endpoint_type": "Interface",
            "billable": True,
            "network_interface_count": 3,
        },
        monthly_cost=None,
    )
    gateway = make_resource(
        "vpce-2",
        resource_type="ec2:vpc-endpoint",
        service="VPC",
        state="available",
        attributes={"endpoint_type": "Gateway", "billable": False},
        monthly_cost=None,
    )

    attribute_costs([interface, gateway], empty_snapshot(), pricing)

    assert interface.monthly_cost == pytest.approx(3 * 0.01 * HOURS_PER_MONTH, abs=0.01)
    # Gateway endpoints for S3 and DynamoDB are free.
    assert gateway.monthly_cost == 0.0


def test_client_vpn_costs_nothing_until_a_subnet_is_associated(tmp_path, settings):
    pricing, _ = build_pricing(CONNECTIVITY_PRICES, tmp_path=tmp_path, settings=settings)
    associated = make_resource(
        "cvpn-1",
        resource_type="ec2:client-vpn-endpoint",
        service="VPC",
        attributes={"associated_subnet_count": 2},
        monthly_cost=None,
    )
    unassociated = make_resource(
        "cvpn-2",
        resource_type="ec2:client-vpn-endpoint",
        service="VPC",
        attributes={"associated_subnet_count": 0},
        monthly_cost=None,
    )

    attribute_costs([associated, unassociated], empty_snapshot(), pricing)

    assert associated.monthly_cost == pytest.approx(2 * 0.10 * HOURS_PER_MONTH, abs=0.01)
    assert unassociated.monthly_cost == 0.0


def test_keys_secrets_alarms_and_registries_are_priced_per_item(tmp_path, settings):
    pricing, _ = build_pricing(CONNECTIVITY_PRICES, tmp_path=tmp_path, settings=settings)
    key = make_resource(
        "key-1", resource_type="kms:key", service="KMS", state="Enabled", monthly_cost=None
    )
    secret = make_resource(
        "prod/db",
        resource_type="secretsmanager:secret",
        service="Secrets Manager",
        state="active",
        monthly_cost=None,
    )
    deleting = make_resource(
        "old/db",
        resource_type="secretsmanager:secret",
        service="Secrets Manager",
        state="pending-deletion",
        monthly_cost=None,
    )
    alarm = make_resource(
        "cpu-high",
        resource_type="cloudwatch:alarm",
        service="CloudWatch",
        attributes={"alarm_kind": "standard"},
        monthly_cost=None,
    )
    repository = make_resource(
        "team/api",
        resource_type="ecr:repository",
        service="ECR",
        attributes={"size_gb": 42.0},
        monthly_cost=None,
    )

    attribute_costs([key, secret, deleting, alarm, repository], empty_snapshot(), pricing)

    assert key.monthly_cost == pytest.approx(1.00)
    assert secret.monthly_cost == pytest.approx(0.40)
    # A secret already scheduled for deletion has stopped being charged for.
    assert deleting.monthly_cost == 0.0
    assert alarm.monthly_cost == pytest.approx(0.10)
    assert repository.monthly_cost == pytest.approx(42.0 * 0.10)


def test_a_registry_whose_size_could_not_be_read_stays_unpriced(tmp_path, settings):
    pricing, _ = build_pricing(CONNECTIVITY_PRICES, tmp_path=tmp_path, settings=settings)
    unreadable = make_resource(
        "team/private",
        resource_type="ecr:repository",
        service="ECR",
        attributes={"size_gb": None},
        monthly_cost=None,
    )
    empty = make_resource(
        "team/new",
        resource_type="ecr:repository",
        service="ECR",
        attributes={"size_gb": 0.0},
        monthly_cost=None,
    )

    attribute_costs([unreadable, empty], empty_snapshot(), pricing)

    # A denied DescribeImages must not read as an empty registry.
    assert unreadable.monthly_cost is None
    assert empty.monthly_cost == 0.0


def test_a_private_certificate_authority_carries_the_charge_not_the_certificate(tmp_path, settings):
    pricing, _ = build_pricing(CONNECTIVITY_PRICES, tmp_path=tmp_path, settings=settings)
    authority = make_resource(
        "ca-1",
        resource_type="acm-pca:certificate-authority",
        service="Certificate Manager",
        state="DISABLED",
        attributes={"billable": True, "usage_mode": "GENERAL_PURPOSE"},
        monthly_cost=None,
    )
    certificate = make_resource(
        "cert-1",
        resource_type="acm:certificate",
        service="Certificate Manager",
        state="ISSUED",
        attributes={"certificate_type": "AMAZON_ISSUED"},
        monthly_cost=None,
    )

    attribute_costs([authority, certificate], empty_snapshot(), pricing)

    # Disabling a private CA does not stop the monthly charge.
    assert authority.monthly_cost == pytest.approx(400.00)
    # Public certificates are free.
    assert certificate.monthly_cost == 0.0


def test_topics_and_queues_are_priced_from_measured_requests(tmp_path, settings):
    pricing, _ = build_pricing(CONNECTIVITY_PRICES, tmp_path=tmp_path, settings=settings)
    topic = make_resource(
        "alerts",
        resource_type="sns:topic",
        service="SNS",
        metrics={"sns_messages_per_month": 2_000_000.0},
        monthly_cost=None,
    )
    idle_topic = make_resource(
        "unused", resource_type="sns:topic", service="SNS", metrics={}, monthly_cost=None
    )
    queue = make_resource(
        "jobs",
        resource_type="sqs:queue",
        service="SQS",
        metrics={"sqs_requests_per_month": 4_000_000.0},
        monthly_cost=None,
    )

    attribute_costs([topic, idle_topic, queue], empty_snapshot(), pricing)

    assert topic.monthly_cost == pytest.approx(2_000_000 * 0.0000005)
    # Neither service has a standing charge, so silence really is free.
    assert idle_topic.monthly_cost == 0.0
    assert queue.monthly_cost == pytest.approx(4_000_000 * 0.0000005)


def test_resources_without_a_defensible_price_stay_unpriced(tmp_path, settings):
    pricing, _ = build_pricing(LIST_PRICES, tmp_path=tmp_path, settings=settings)
    unknown = make_resource(
        "some-thing", resource_type="sagemaker:endpoint", service="SageMaker", monthly_cost=None
    )

    attribute_costs([unknown], empty_snapshot(), pricing)

    assert unknown.monthly_cost is None
    assert unknown.cost_basis is None


def test_an_unreachable_price_list_leaves_resources_unpriced_and_says_why(tmp_path, settings):
    pricing, _ = build_pricing(fail=True, tmp_path=tmp_path, settings=settings)
    volume = make_resource(
        "vol-1",
        resource_type="ebs:volume",
        service="EBS",
        attributes={"volume_type": "gp2", "size_gb": 200},
        monthly_cost=None,
    )

    attribute_costs([volume], empty_snapshot(), pricing)

    assert volume.monthly_cost is None
    assert volume.cost_basis is None
    note = next(n for n in pricing.notes.notes if n.capability == "pricing:list-price-estimates")
    assert "1 resources were left unpriced" in note.message
    assert "pricing:GetProducts" in (note.remedy or "")
