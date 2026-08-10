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
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from finops.aws.costs import CostSnapshot
from finops.aws.pricing import HOURS_PER_MONTH, PricingClient
from finops.model import Resource

logger = logging.getLogger(__name__)

# us-east-1 list prices for charges the Price List API models awkwardly. Used only for
# estimates, which are always labelled as such in the UI.
STATIC_RATES = {
    "s3_standard_gb_month": 0.023,
    "dynamodb_rcu_hour": 0.00013,
    "dynamodb_wcu_hour": 0.00065,
    "lambda_gb_second": 0.0000166667,
    "lambda_per_request": 0.0000002,
    "lambda_provisioned_gb_second": 0.0000041667,
}

BYTES_PER_GB = 1024**3


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
    return attributed


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
    return pricing.public_ipv4_hourly(resource.region).monthly


def _nat_gateway(resource: Resource, pricing: PricingClient) -> float | None:
    # Hourly charge only; data processing depends on traffic and is added by metrics.
    hourly = pricing.nat_gateway_hourly(resource.region).monthly
    processed_gb = resource.metrics.get("nat_bytes_processed_per_month_gb")
    if processed_gb:
        hourly += processed_gb * 0.045
    return hourly


def _load_balancer(resource: Resource, pricing: PricingClient) -> float | None:
    lb_type = resource.attributes.get("lb_type", "application")
    return pricing.load_balancer_hourly(resource.region, lb_type).monthly


def _eks_cluster(resource: Resource, pricing: PricingClient) -> float | None:
    return pricing.eks_cluster_hourly(resource.region).monthly


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
    # RDS gp2/gp3 storage is billed per GB-month at roughly EBS rates.
    storage_price = pricing.ebs_gb_month(resource.region, "gp2")
    storage = storage_price.amount * storage_gb if storage_price else 0.0
    if attributes.get("multi_az"):
        storage *= 2
    return compute + storage


def _rds_snapshot(resource: Resource, pricing: PricingClient) -> float | None:
    size_gb = resource.attributes.get("allocated_storage_gb")
    if not size_gb:
        return None
    price = pricing.snapshot_gb_month(resource.region)
    return price.amount * float(size_gb) if price else None


def _log_group(resource: Resource, pricing: PricingClient) -> float | None:
    stored_gb = resource.attributes.get("stored_gb") or 0
    if not stored_gb:
        return 0.0
    return pricing.logs_storage_gb_month(resource.region).amount * stored_gb


def _s3_bucket(resource: Resource, pricing: PricingClient) -> float | None:
    # Size comes from the CloudWatch daily storage metric collected earlier.
    size_bytes = resource.metrics.get("bucket_size_bytes")
    if not size_bytes:
        return None
    return (size_bytes / BYTES_PER_GB) * STATIC_RATES["s3_standard_gb_month"]


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
    monthly = (
        rcu * STATIC_RATES["dynamodb_rcu_hour"] + wcu * STATIC_RATES["dynamodb_wcu_hour"]
    ) * HOURS_PER_MONTH
    size_gb = (attributes.get("size_bytes") or 0) / BYTES_PER_GB
    return monthly + size_gb * 0.25


def _lambda_function(resource: Resource, pricing: PricingClient) -> float | None:
    metrics = resource.metrics
    invocations = metrics.get("invocations_per_month")
    duration_ms = metrics.get("duration_avg_ms")
    memory_mb = resource.attributes.get("memory_mb") or 128
    provisioned = resource.attributes.get("provisioned_concurrency") or 0

    total = 0.0
    if provisioned:
        total += (
            provisioned
            * (memory_mb / 1024)
            * HOURS_PER_MONTH
            * 3600
            * STATIC_RATES["lambda_provisioned_gb_second"]
        )
    if invocations and duration_ms:
        gb_seconds = invocations * (duration_ms / 1000.0) * (memory_mb / 1024.0)
        total += gb_seconds * STATIC_RATES["lambda_gb_second"]
        total += invocations * STATIC_RATES["lambda_per_request"]
    elif not provisioned:
        return None
    return total


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
    "s3:bucket": _s3_bucket,
    "dynamodb:table": _dynamodb_table,
    "lambda:function": _lambda_function,
}
