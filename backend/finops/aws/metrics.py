"""Utilization signals from CloudWatch.

Cost data alone cannot tell you whether a resource is worth what it costs, so every
rightsizing and idle rule needs utilization. ``GetMetricData`` accepts up to 500 queries
per call, so metrics for a whole region are requested in a handful of round trips rather
than one call per resource.

A daily period keeps the response small (14 points per metric over a two week window)
and is sufficient for the "is this thing doing anything at all" question the rules ask.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from statistics import fmean
from typing import Any, Literal

from finops.aws.errors import NoteCollector, graceful
from finops.aws.session import AwsContext
from finops.model import Resource
from finops.util import chunked

logger = logging.getLogger(__name__)

# Hard API limit for a single GetMetricData request.
MAX_QUERIES_PER_CALL = 500
DAILY_PERIOD_SECONDS = 86400
DAYS_PER_MONTH = 30.44

Aggregate = Literal["avg", "sum", "max", "min", "last"]


@dataclass(frozen=True)
class MetricSpec:
    """One CloudWatch metric to fetch and where to store the reduced result."""

    output: str
    namespace: str
    metric_name: str
    dimensions: dict[str, str]
    stat: str = "Average"
    aggregate: Aggregate = "avg"
    scale: float = 1.0
    period: int = DAILY_PERIOD_SECONDS


@dataclass
class _PendingQuery:
    query_id: str
    resource_arn: str
    spec: MetricSpec


_AGGREGATORS: dict[str, Callable[[Sequence[float]], float]] = {
    "avg": fmean,
    "sum": sum,
    "max": max,
    "min": min,
    # Values come back newest-first, so "last" means the most recent reading.
    "last": lambda values: values[0],
}


class MetricsCollector:
    """Fetches and reduces CloudWatch metrics for a set of resources."""

    def __init__(
        self,
        aws: AwsContext,
        notes: NoteCollector | None = None,
        lookback_days: int | None = None,
    ) -> None:
        self.aws = aws
        self.notes = notes or NoteCollector()
        self.lookback_days = lookback_days or aws.settings.metric_lookback_days

    def collect(self, resources: Iterable[Resource]) -> list[Resource]:
        """Populate ``resource.metrics`` in place and return the same resources."""
        by_arn = {r.arn: r for r in resources}
        by_region: dict[str, list[Resource]] = {}
        for resource in by_arn.values():
            by_region.setdefault(resource.region, []).append(resource)

        if not by_region:
            return list(by_arn.values())

        workers = min(self.aws.settings.max_workers, len(by_region))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="metrics") as pool:
            results = pool.map(
                lambda item: self._collect_region(item[0], item[1]), by_region.items()
            )
            for region_results in results:
                for arn, values in region_results.items():
                    resource = by_arn.get(arn)
                    if resource is not None:
                        resource.metrics.update(values)

        self._derive(by_arn.values())
        return list(by_arn.values())

    def _collect_region(
        self, region: str, resources: Sequence[Resource]
    ) -> dict[str, dict[str, float]]:
        pending: list[_PendingQuery] = []
        for index, resource in enumerate(resources):
            for spec_index, spec in enumerate(build_specs(resource)):
                # Query ids must start with a lowercase letter and stay alphanumeric.
                pending.append(_PendingQuery(f"q{index}_{spec_index}", resource.arn, spec))

        if not pending:
            return {}

        end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        start = end - timedelta(days=self.lookback_days)
        collected: dict[str, dict[str, float]] = {}

        for batch in chunked(pending, MAX_QUERIES_PER_CALL):
            with graceful(self.notes, "cloudwatch:GetMetricData", region=region):
                for query_id, values in self._fetch(region, batch, start, end).items():
                    query = next(q for q in batch if q.query_id == query_id)
                    reduced = _reduce(values, query.spec)
                    if reduced is None:
                        continue
                    collected.setdefault(query.resource_arn, {})[query.spec.output] = reduced
        return collected

    def _fetch(
        self,
        region: str,
        batch: Sequence[_PendingQuery],
        start: datetime,
        end: datetime,
    ) -> dict[str, list[float]]:
        client = self.aws.client("cloudwatch", region)
        queries = [
            {
                "Id": query.query_id,
                "MetricStat": {
                    "Metric": {
                        "Namespace": query.spec.namespace,
                        "MetricName": query.spec.metric_name,
                        "Dimensions": [
                            {"Name": name, "Value": value}
                            for name, value in query.spec.dimensions.items()
                        ],
                    },
                    "Period": query.spec.period,
                    "Stat": query.spec.stat,
                },
                "ReturnData": True,
            }
            for query in batch
        ]

        values: dict[str, list[float]] = {}
        next_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {
                "MetricDataQueries": queries,
                "StartTime": start,
                "EndTime": end,
                "ScanBy": "TimestampDescending",
            }
            if next_token:
                kwargs["NextToken"] = next_token
            response = client.get_metric_data(**kwargs)
            for result in response.get("MetricDataResults", []):
                values.setdefault(result["Id"], []).extend(result.get("Values", []))
            next_token = response.get("NextToken")
            if not next_token:
                break
        return values

    def _derive(self, resources: Iterable[Resource]) -> None:
        """Compute values that combine a metric with the resource's configuration."""
        for resource in resources:
            metrics = resource.metrics
            attributes = resource.attributes

            if resource.resource_type == "dynamodb:table":
                provisioned_read = attributes.get("read_capacity_units") or 0
                provisioned_write = attributes.get("write_capacity_units") or 0
                if provisioned_read:
                    metrics["read_utilization_percent"] = round(
                        metrics.get("consumed_rcu_per_second", 0.0) / provisioned_read * 100, 2
                    )
                if provisioned_write:
                    metrics["write_utilization_percent"] = round(
                        metrics.get("consumed_wcu_per_second", 0.0) / provisioned_write * 100, 2
                    )

            if resource.resource_type == "ec2:nat-gateway":
                bytes_per_day = metrics.get("nat_bytes_per_day")
                if bytes_per_day is not None:
                    metrics["nat_bytes_processed_per_month_gb"] = round(
                        bytes_per_day * DAYS_PER_MONTH / 1024**3, 4
                    )

            if resource.resource_type == "ec2:instance":
                network_in = metrics.get("network_in_bytes_per_day")
                network_out = metrics.get("network_out_bytes_per_day")
                if network_in is not None or network_out is not None:
                    metrics["network_bytes_per_day"] = round(
                        (network_in or 0.0) + (network_out or 0.0), 2
                    )

            if resource.resource_type == "ebs:volume":
                read_ops = metrics.get("volume_read_ops_per_day")
                write_ops = metrics.get("volume_write_ops_per_day")
                if read_ops is not None or write_ops is not None:
                    total = (read_ops or 0.0) + (write_ops or 0.0)
                    metrics["volume_ops_per_day"] = round(total, 2)
                    metrics["volume_iops_observed"] = round(total / DAILY_PERIOD_SECONDS, 4)


def _reduce(values: Sequence[float], spec: MetricSpec) -> float | None:
    if not values:
        return None
    aggregator = _AGGREGATORS[spec.aggregate]
    return round(aggregator(values) * spec.scale, 4)


# ------------------------------------------------------------------ query specs


def _alb_dimension(arn: str) -> str | None:
    """CloudWatch identifies a load balancer by the tail of its ARN, e.g. app/name/id."""
    match = re.search(r"loadbalancer/(.+)$", arn)
    return match.group(1) if match else None


def _ec2_specs(resource: Resource) -> list[MetricSpec]:
    if resource.state != "running":
        return []
    dimensions = {"InstanceId": resource.resource_id}
    return [
        MetricSpec("cpu_avg", "AWS/EC2", "CPUUtilization", dimensions, "Average"),
        MetricSpec("cpu_max", "AWS/EC2", "CPUUtilization", dimensions, "Maximum", "max"),
        MetricSpec("cpu_p95", "AWS/EC2", "CPUUtilization", dimensions, "p95"),
        MetricSpec("network_in_bytes_per_day", "AWS/EC2", "NetworkIn", dimensions, "Sum", "avg"),
        MetricSpec("network_out_bytes_per_day", "AWS/EC2", "NetworkOut", dimensions, "Sum", "avg"),
    ]


def _ebs_specs(resource: Resource) -> list[MetricSpec]:
    # Detached volumes emit no metrics at all.
    if resource.state != "in-use":
        return []
    dimensions = {"VolumeId": resource.resource_id}
    return [
        MetricSpec("volume_read_ops_per_day", "AWS/EBS", "VolumeReadOps", dimensions, "Sum", "avg"),
        MetricSpec(
            "volume_write_ops_per_day", "AWS/EBS", "VolumeWriteOps", dimensions, "Sum", "avg"
        ),
        MetricSpec(
            "volume_idle_seconds_per_day", "AWS/EBS", "VolumeIdleTime", dimensions, "Sum", "avg"
        ),
    ]


def _elbv2_specs(resource: Resource) -> list[MetricSpec]:
    dimension_value = _alb_dimension(resource.arn)
    if not dimension_value:
        return []
    dimensions = {"LoadBalancer": dimension_value}
    lb_type = resource.attributes.get("lb_type", "application")
    if lb_type == "application":
        return [
            MetricSpec(
                "requests_per_day",
                "AWS/ApplicationELB",
                "RequestCount",
                dimensions,
                "Sum",
                "avg",
            ),
            MetricSpec(
                "processed_bytes_per_day",
                "AWS/ApplicationELB",
                "ProcessedBytes",
                dimensions,
                "Sum",
                "avg",
            ),
        ]
    if lb_type == "network":
        return [
            MetricSpec(
                "requests_per_day", "AWS/NetworkELB", "NewFlowCount", dimensions, "Sum", "avg"
            ),
            MetricSpec(
                "processed_bytes_per_day",
                "AWS/NetworkELB",
                "ProcessedBytes",
                dimensions,
                "Sum",
                "avg",
            ),
        ]
    return []


def _classic_elb_specs(resource: Resource) -> list[MetricSpec]:
    dimensions = {"LoadBalancerName": resource.resource_id}
    return [MetricSpec("requests_per_day", "AWS/ELB", "RequestCount", dimensions, "Sum", "avg")]


def _nat_specs(resource: Resource) -> list[MetricSpec]:
    dimensions = {"NatGatewayId": resource.resource_id}
    return [
        MetricSpec(
            "nat_bytes_per_day", "AWS/NATGateway", "BytesOutToDestination", dimensions, "Sum", "avg"
        ),
        MetricSpec(
            "nat_active_connections",
            "AWS/NATGateway",
            "ActiveConnectionCount",
            dimensions,
            "Maximum",
            "max",
        ),
    ]


def _rds_specs(resource: Resource) -> list[MetricSpec]:
    dimensions = {"DBInstanceIdentifier": resource.resource_id}
    return [
        MetricSpec("cpu_avg", "AWS/RDS", "CPUUtilization", dimensions, "Average"),
        MetricSpec("cpu_max", "AWS/RDS", "CPUUtilization", dimensions, "Maximum", "max"),
        MetricSpec("db_connections_avg", "AWS/RDS", "DatabaseConnections", dimensions, "Average"),
        MetricSpec(
            "db_connections_max", "AWS/RDS", "DatabaseConnections", dimensions, "Maximum", "max"
        ),
        MetricSpec(
            "free_storage_bytes_min", "AWS/RDS", "FreeStorageSpace", dimensions, "Minimum", "min"
        ),
        MetricSpec("read_iops_avg", "AWS/RDS", "ReadIOPS", dimensions, "Average"),
        MetricSpec("write_iops_avg", "AWS/RDS", "WriteIOPS", dimensions, "Average"),
    ]


def _lambda_specs(resource: Resource) -> list[MetricSpec]:
    dimensions = {"FunctionName": resource.resource_id}
    return [
        MetricSpec(
            "invocations_per_month",
            "AWS/Lambda",
            "Invocations",
            dimensions,
            "Sum",
            "avg",
            scale=DAYS_PER_MONTH,
        ),
        MetricSpec("duration_avg_ms", "AWS/Lambda", "Duration", dimensions, "Average"),
        MetricSpec("duration_max_ms", "AWS/Lambda", "Duration", dimensions, "Maximum", "max"),
        MetricSpec("errors_per_day", "AWS/Lambda", "Errors", dimensions, "Sum", "avg"),
    ]


def _s3_specs(resource: Resource) -> list[MetricSpec]:
    return [
        MetricSpec(
            "bucket_size_bytes",
            "AWS/S3",
            "BucketSizeBytes",
            {"BucketName": resource.resource_id, "StorageType": "StandardStorage"},
            "Average",
            "last",
        ),
        MetricSpec(
            "object_count",
            "AWS/S3",
            "NumberOfObjects",
            {"BucketName": resource.resource_id, "StorageType": "AllStorageTypes"},
            "Average",
            "last",
        ),
    ]


def _dynamodb_specs(resource: Resource) -> list[MetricSpec]:
    if resource.attributes.get("billing_mode") != "PROVISIONED":
        return []
    dimensions = {"TableName": resource.resource_id}
    # Daily sums divided by seconds in a day give the average consumed capacity per
    # second, which is the unit provisioned capacity is expressed in.
    per_second = 1.0 / DAILY_PERIOD_SECONDS
    return [
        MetricSpec(
            "consumed_rcu_per_second",
            "AWS/DynamoDB",
            "ConsumedReadCapacityUnits",
            dimensions,
            "Sum",
            "avg",
            scale=per_second,
        ),
        MetricSpec(
            "consumed_wcu_per_second",
            "AWS/DynamoDB",
            "ConsumedWriteCapacityUnits",
            dimensions,
            "Sum",
            "avg",
            scale=per_second,
        ),
    ]


_SPEC_BUILDERS: dict[str, Callable[[Resource], list[MetricSpec]]] = {
    "ec2:instance": _ec2_specs,
    "ebs:volume": _ebs_specs,
    "elbv2:application": _elbv2_specs,
    "elbv2:network": _elbv2_specs,
    "elb:classic": _classic_elb_specs,
    "ec2:nat-gateway": _nat_specs,
    "rds:db-instance": _rds_specs,
    "lambda:function": _lambda_specs,
    "s3:bucket": _s3_specs,
    "dynamodb:table": _dynamodb_specs,
}


def build_specs(resource: Resource) -> list[MetricSpec]:
    builder = _SPEC_BUILDERS.get(resource.resource_type)
    return builder(resource) if builder else []
