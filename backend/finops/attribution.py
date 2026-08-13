"""Assign a monthly cost to every resource.

Order of preference:

1. Billed cost from ``GetCostAndUsageWithResources`` when the account has resource-level
   data enabled. This is real money and is labelled ``actual_resource_level``.
2. A list-price estimate computed from the Price List API, labelled
   ``list_price_estimate``.
3. Nothing. A resource with no defensible number keeps ``monthly_cost = None`` rather
   than being given a made-up figure.

The account total always comes from Cost Explorer, never from summing these estimates,
so an incomplete attribution understates per-resource detail without corrupting the TCO.

Every rate below is fetched from the Price List API for the resource's own region. None
are written down here: a resource whose price cannot be fetched stays unpriced.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from finops.aws.costs import CostSnapshot
from finops.aws.pricing import HOURS_PER_MONTH, PricingClient
from finops.model import Resource

logger = logging.getLogger(__name__)

BYTES_PER_GB = 1024**3
SECONDS_PER_MONTH = HOURS_PER_MONTH * 3600


def attribute_costs(
    resources: Iterable[Resource], snapshot: CostSnapshot, pricing: PricingClient
) -> list[Resource]:
    """Populate ``monthly_cost`` and ``cost_basis`` on each resource, in place."""
    attributed = list(resources)
    for resource in attributed:
        # Reset first so re-running attribution never leaves a stale figure behind.
        resource.monthly_cost = None
        resource.cost_basis = None

        billed = snapshot.monthly_cost_for_resource(
            resource.arn, resource.resource_id, _arn_tail(resource.arn)
        )
        if billed is not None:
            resource.monthly_cost = round(billed, 2)
            resource.cost_basis = "actual_resource_level"
            continue

        estimate = _estimate(resource, pricing)
        if estimate is not None:
            resource.monthly_cost = round(estimate, 2)
            resource.cost_basis = "list_price_estimate"

    pricing.save_cache()
    priced = sum(1 for r in attributed if r.monthly_cost is not None)
    logger.info("Attributed cost to %d of %d resources", priced, len(attributed))
    _note_pricing_gaps(pricing, len(attributed) - priced)
    return attributed


def _note_pricing_gaps(pricing: PricingClient, unpriced: int) -> None:
    """Say what an unavailable price list cost us, so blanks in the UI are explained."""
    if pricing.used_public_price_list:
        pricing.notes.add(
            "pricing:list-price-estimates",
            "partial",
            "The Price List API was unavailable, so list prices came from the price list "
            "files AWS publishes without authentication. Same published rates.",
            remedy="Grant pricing:GetProducts to use the API instead, which needs no "
            "download and always reflects today's prices.",
        )
    elif unpriced and not pricing.api_available:
        pricing.notes.add(
            "pricing:list-price-estimates",
            "denied",
            f"{unpriced} resources were left unpriced because the Price List API could "
            "not be reached. Costs are never estimated from built-in rates.",
            remedy="Grant pricing:GetProducts and pricing:DescribeServices; the data is "
            "public list pricing and needs no Cost Explorer access.",
        )
    if pricing.unresolved:
        pricing.notes.add(
            "pricing:list-price-estimates",
            "unavailable",
            "The Price List API published nothing matching "
            + ", ".join(pricing.unresolved[:6])
            + ", so those charges are unpriced.",
            remedy="AWS has changed how it publishes these charges; the price filters in "
            "backend/finops/aws/pricing.py need updating.",
        )


def _arn_tail(arn: str) -> str | None:
    if "/" in arn:
        return arn.rsplit("/", 1)[-1]
    if ":" in arn:
        return arn.rsplit(":", 1)[-1]
    return None


def _estimate(resource: Resource, pricing: PricingClient) -> float | None:
    handler = _ESTIMATORS.get(resource.resource_type)
    return handler(resource, pricing) if handler else None


# ------------------------------------------------------------------ estimators


def _ec2_instance(resource: Resource, pricing: PricingClient) -> float | None:
    # A stopped instance costs nothing for compute; its attached volumes still bill and
    # are counted against those volumes.
    if resource.state != "running":
        return 0.0
    attributes = resource.attributes
    if attributes.get("lifecycle") == "spot":
        # Spot pricing is dynamic; a list price would overstate it substantially.
        return None
    instance_type = attributes.get("instance_type")
    if not instance_type:
        return None
    return pricing.ec2_instance_monthly(
        resource.region,
        instance_type,
        operating_system=attributes.get("platform_details") or "Linux",
    )


def _ebs_volume(resource: Resource, pricing: PricingClient) -> float | None:
    attributes = resource.attributes
    volume_type = attributes.get("volume_type")
    size_gb = attributes.get("size_gb")
    if not volume_type or not size_gb:
        return None
    return pricing.ebs_volume_monthly(
        resource.region,
        volume_type,
        float(size_gb),
        iops=attributes.get("iops"),
        throughput_mibps=attributes.get("throughput_mibps"),
    )


def _ebs_snapshot(resource: Resource, pricing: PricingClient) -> float | None:
    # Snapshots bill on incremental blocks, which no API exposes cheaply. Full volume
    # size is the upper bound and the figure AWS itself uses in its own estimates.
    size_gb = resource.attributes.get("volume_size_gb")
    if not size_gb:
        return None
    price = pricing.snapshot_gb_month(resource.region)
    return price.amount * float(size_gb) if price else None


def _ami(resource: Resource, pricing: PricingClient) -> float | None:
    # An AMI has no charge of its own; its backing snapshots are inventoried separately,
    # so pricing it here would double count.
    return 0.0


def _elastic_ip(resource: Resource, pricing: PricingClient) -> float | None:
    # Every public IPv4 address bills, attached or not, at its own published rate.
    in_use = bool(resource.attributes.get("associated"))
    price = pricing.public_ipv4_hourly(resource.region, in_use=in_use)
    return price.monthly if price else None


def _nat_gateway(resource: Resource, pricing: PricingClient) -> float | None:
    hourly = pricing.nat_gateway_hourly(resource.region)
    if hourly is None:
        return None
    total = hourly.monthly
    # Data processing depends on traffic, which the metrics collector supplies.
    processed_gb = resource.metrics.get("nat_bytes_processed_per_month_gb")
    if processed_gb:
        per_gb = pricing.nat_gateway_gb(resource.region)
        if per_gb:
            total += processed_gb * per_gb.amount
    return total


def _load_balancer(resource: Resource, pricing: PricingClient) -> float | None:
    lb_type = resource.attributes.get("lb_type", "application")
    price = pricing.load_balancer_hourly(resource.region, lb_type)
    return price.monthly if price else None


def _eks_cluster(resource: Resource, pricing: PricingClient) -> float | None:
    price = pricing.eks_cluster_hourly(resource.region)
    return price.monthly if price else None


def _eks_nodegroup(resource: Resource, pricing: PricingClient) -> float | None:
    attributes = resource.attributes
    instance_types = attributes.get("instance_types") or []
    desired = attributes.get("desired_size") or 0
    if not instance_types or not desired:
        return None
    hourly = pricing.ec2_instance_hourly(resource.region, instance_types[0])
    if hourly is None:
        return None
    monthly = hourly.monthly * desired
    if attributes.get("capacity_type") == "SPOT":
        # Spot typically lands around 30% of on-demand; flag the approximation by
        # keeping it conservative rather than precise.
        monthly *= 0.35
    return monthly


def _rds_instance(resource: Resource, pricing: PricingClient) -> float | None:
    attributes = resource.attributes
    instance_class = attributes.get("instance_class")
    engine = attributes.get("engine")
    if not instance_class or not engine:
        return None
    compute = pricing.rds_instance_monthly(
        resource.region, instance_class, engine, multi_az=bool(attributes.get("multi_az"))
    )
    if compute is None:
        return None
    storage_gb = attributes.get("allocated_storage_gb") or 0
    multi_az = bool(attributes.get("multi_az"))
    # RDS publishes its own storage rates, and the Multi-AZ rate already covers both
    # copies, so it must not be doubled here.
    storage_price = pricing.rds_storage_gb_month(
        resource.region, attributes.get("storage_type") or "gp2", multi_az=multi_az
    )
    storage = storage_price.amount * storage_gb if storage_price else 0.0
    return compute + storage


def _rds_snapshot(resource: Resource, pricing: PricingClient) -> float | None:
    size_gb = resource.attributes.get("allocated_storage_gb")
    if not size_gb:
        return None
    price = pricing.rds_backup_gb_month(resource.region)
    return price.amount * float(size_gb) if price else None


def _log_group(resource: Resource, pricing: PricingClient) -> float | None:
    stored_gb = resource.attributes.get("stored_gb") or 0
    if not stored_gb:
        return 0.0
    price = pricing.logs_storage_gb_month(resource.region)
    return price.amount * stored_gb if price else None


def _efs_file_system(resource: Resource, pricing: PricingClient) -> float | None:
    # Sizes per tier come from the file system itself, so the storage half of this is
    # measured rather than estimated.
    attributes = resource.attributes
    return pricing.efs_file_system_monthly(
        resource.region,
        standard_gb=float(attributes.get("standard_gb") or 0.0),
        ia_gb=float(attributes.get("ia_gb") or 0.0),
        archive_gb=float(attributes.get("archive_gb") or 0.0),
        one_zone=bool(attributes.get("one_zone")),
        throughput_mode=attributes.get("throughput_mode") or "bursting",
        provisioned_mibps=attributes.get("provisioned_throughput_mibps"),
    )


def _s3_bucket(resource: Resource, pricing: PricingClient) -> float | None:
    # Size comes from the CloudWatch daily storage metric collected earlier.
    size_bytes = resource.metrics.get("bucket_size_bytes")
    if not size_bytes:
        return None
    price = pricing.s3_standard_gb_month(resource.region)
    return (size_bytes / BYTES_PER_GB) * price.amount if price else None


def _dynamodb_table(resource: Resource, pricing: PricingClient) -> float | None:
    attributes = resource.attributes
    if attributes.get("billing_mode") != "PROVISIONED":
        # On-demand tables bill per request; the metrics layer supplies the volume.
        return None
    rcu = attributes.get("read_capacity_units") or 0
    wcu = attributes.get("write_capacity_units") or 0
    for index in attributes.get("global_secondary_indexes", []):
        rcu += index.get("read_capacity_units") or 0
        wcu += index.get("write_capacity_units") or 0

    read_price = pricing.dynamodb_capacity_hourly(resource.region, "read")
    write_price = pricing.dynamodb_capacity_hourly(resource.region, "write")
    if read_price is None or write_price is None:
        return None
    monthly = (rcu * read_price.amount + wcu * write_price.amount) * HOURS_PER_MONTH

    size_gb = (attributes.get("size_bytes") or 0) / BYTES_PER_GB
    if size_gb:
        storage_price = pricing.dynamodb_storage_gb_month(resource.region)
        if storage_price:
            monthly += size_gb * storage_price.amount
    return monthly


def _lambda_function(resource: Resource, pricing: PricingClient) -> float | None:
    metrics = resource.metrics
    invocations = metrics.get("invocations_per_month")
    duration_ms = metrics.get("duration_avg_ms")
    memory_mb = resource.attributes.get("memory_mb") or 128
    provisioned = resource.attributes.get("provisioned_concurrency") or 0

    if not provisioned and not (invocations and duration_ms):
        return None

    total = 0.0
    if provisioned:
        price = pricing.lambda_gb_second(resource.region, provisioned=True)
        if price is None:
            return None
        total += provisioned * (memory_mb / 1024) * SECONDS_PER_MONTH * price.amount
    if invocations and duration_ms:
        duration_price = pricing.lambda_gb_second(resource.region)
        request_price = pricing.lambda_request(resource.region)
        if duration_price is None or request_price is None:
            return None
        gb_seconds = invocations * (duration_ms / 1000.0) * (memory_mb / 1024.0)
        total += gb_seconds * duration_price.amount
        total += invocations * request_price.amount
    return total


def _transit_gateway(resource: Resource, pricing: PricingClient) -> float | None:
    # The gateway itself is free; every charge hangs off its attachments. A gateway shared
    # in from another account is not this account's cost either way.
    return 0.0


def _transit_gateway_attachment(resource: Resource, pricing: PricingClient) -> float | None:
    attributes = resource.attributes
    if not attributes.get("owned_by_this_account"):
        # Whoever attached the resource pays for the attachment, and that is not us.
        return 0.0
    if resource.state != "available":
        return 0.0
    hourly = pricing.transit_gateway_attachment_hourly(
        resource.region, attributes.get("attachment_kind") or "vpc"
    )
    if hourly is None:
        return None
    total = hourly.monthly
    processed_gb = resource.metrics.get("tgw_bytes_processed_per_month_gb")
    if processed_gb:
        per_gb = pricing.transit_gateway_gb(
            resource.region, attributes.get("attachment_kind") or "vpc"
        )
        if per_gb:
            total += processed_gb * per_gb.amount
    return total


def _vpc_endpoint(resource: Resource, pricing: PricingClient) -> float | None:
    attributes = resource.attributes
    # Gateway endpoints for S3 and DynamoDB carry no charge at all.
    if not attributes.get("billable"):
        return 0.0
    gwlb = attributes.get("endpoint_type") == "GatewayLoadBalancer"
    hourly = pricing.vpc_endpoint_hourly(resource.region, gateway_load_balancer=gwlb)
    if hourly is None:
        return None
    # Charged per network interface, which means once per availability zone it serves.
    interfaces = attributes.get("network_interface_count") or 1
    total = hourly.monthly * interfaces
    processed_gb = resource.metrics.get("endpoint_bytes_processed_per_month_gb")
    if processed_gb:
        per_gb = pricing.vpc_endpoint_gb(resource.region, gateway_load_balancer=gwlb)
        if per_gb:
            total += processed_gb * per_gb.amount
    return total


def _vpn_connection(resource: Resource, pricing: PricingClient) -> float | None:
    if resource.state not in {"available", "pending"}:
        return 0.0
    price = pricing.vpn_connection_hourly(resource.region)
    return price.monthly if price else None


def _client_vpn_endpoint(resource: Resource, pricing: PricingClient) -> float | None:
    subnets = resource.attributes.get("associated_subnet_count") or 0
    if not subnets:
        # Nothing is charged until a subnet is associated.
        return 0.0
    price = pricing.client_vpn_endpoint_hourly(resource.region)
    return price.monthly * subnets if price else None


def _kms_key(resource: Resource, pricing: PricingClient) -> float | None:
    # Keys already deleted stop billing; ones merely awaiting deletion do not.
    if resource.state == "Unavailable":
        return 0.0
    price = pricing.kms_key_month(resource.region)
    return price.amount if price else None


def _secret(resource: Resource, pricing: PricingClient) -> float | None:
    if resource.state == "pending-deletion":
        return 0.0
    price = pricing.secret_month(resource.region)
    return price.amount if price else None


def _certificate(resource: Resource, pricing: PricingClient) -> float | None:
    # Public certificates are free, and private ones are charged when issued rather than
    # held, so the standing cost belongs to the authority.
    return 0.0


def _certificate_authority(resource: Resource, pricing: PricingClient) -> float | None:
    attributes = resource.attributes
    if not attributes.get("billable"):
        return 0.0
    short_lived = attributes.get("usage_mode") == "SHORT_LIVED_CERTIFICATE"
    price = pricing.private_ca_month(resource.region, short_lived=short_lived)
    return price.amount if price else None


def _ecr_repository(resource: Resource, pricing: PricingClient) -> float | None:
    size_gb = resource.attributes.get("size_gb")
    # An unreadable registry is left unpriced; an empty one really does cost nothing.
    if size_gb is None:
        return None
    if not size_gb:
        return 0.0
    price = pricing.ecr_storage_gb_month(resource.region)
    return price.amount * size_gb if price else None


def _cloudwatch_alarm(resource: Resource, pricing: PricingClient) -> float | None:
    price = pricing.cloudwatch_alarm_month(
        resource.region, resource.attributes.get("alarm_kind") or "standard"
    )
    return price.amount if price else None


def _sns_topic(resource: Resource, pricing: PricingClient) -> float | None:
    # No standing charge: an idle topic is free, and the bill is the messages published
    # plus delivery to endpoints that charge for it.
    published = resource.metrics.get("sns_messages_per_month")
    if published is None:
        return 0.0
    price = pricing.sns_request(resource.region)
    return published * price.amount if price else None


def _sqs_queue(resource: Resource, pricing: PricingClient) -> float | None:
    # Also request-priced. Empty receives count, which is why long polling matters.
    requests = resource.metrics.get("sqs_requests_per_month")
    if requests is None:
        return 0.0
    price = pricing.sqs_request(resource.region, fifo=bool(resource.attributes.get("fifo")))
    return requests * price.amount if price else None


_ESTIMATORS = {
    "ec2:instance": _ec2_instance,
    "ebs:volume": _ebs_volume,
    "ebs:snapshot": _ebs_snapshot,
    "ec2:image": _ami,
    "ec2:elastic-ip": _elastic_ip,
    "ec2:nat-gateway": _nat_gateway,
    "elbv2:application": _load_balancer,
    "elbv2:network": _load_balancer,
    "elbv2:gateway": _load_balancer,
    "elb:classic": _load_balancer,
    "eks:cluster": _eks_cluster,
    "eks:nodegroup": _eks_nodegroup,
    "rds:db-instance": _rds_instance,
    "rds:snapshot": _rds_snapshot,
    "logs:log-group": _log_group,
    "efs:file-system": _efs_file_system,
    "s3:bucket": _s3_bucket,
    "dynamodb:table": _dynamodb_table,
    "lambda:function": _lambda_function,
    "ec2:transit-gateway": _transit_gateway,
    "ec2:transit-gateway-attachment": _transit_gateway_attachment,
    "ec2:vpc-endpoint": _vpc_endpoint,
    "ec2:vpn-connection": _vpn_connection,
    "ec2:client-vpn-endpoint": _client_vpn_endpoint,
    "kms:key": _kms_key,
    "secretsmanager:secret": _secret,
    "acm:certificate": _certificate,
    "acm-pca:certificate-authority": _certificate_authority,
    "ecr:repository": _ecr_repository,
    "cloudwatch:alarm": _cloudwatch_alarm,
    "sns:topic": _sns_topic,
    "sqs:queue": _sqs_queue,
}
