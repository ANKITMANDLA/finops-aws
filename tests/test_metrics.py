from __future__ import annotations

from typing import Any

import pytest
from tests.factories import make_resource
from tests.fakes import FakeAwsContext, client_error

from finops.aws.errors import NoteCollector
from finops.aws.metrics import (
    MAX_QUERIES_PER_CALL,
    MetricsCollector,
    _alb_dimension,
    build_specs,
)


class FakeCloudWatchClient:
    """Returns a fixed series for every query, and records how it was called."""

    def __init__(self, series_by_metric: dict[str, list[float]] | None = None, *, fail=False):
        self.series_by_metric = series_by_metric or {}
        self.fail = fail
        self.requests: list[dict[str, Any]] = []

    def get_metric_data(self, **kwargs: Any) -> dict[str, Any]:
        if self.fail:
            raise client_error("AccessDenied", "GetMetricData")
        self.requests.append(kwargs)
        results = []
        for query in kwargs["MetricDataQueries"]:
            metric_name = query["MetricStat"]["Metric"]["MetricName"]
            values = self.series_by_metric.get(metric_name, [])
            results.append({"Id": query["Id"], "Values": list(values)})
        return {"MetricDataResults": results}

    @property
    def query_count(self) -> int:
        return sum(len(request["MetricDataQueries"]) for request in self.requests)


def collector(client, settings, lookback_days=14):
    return MetricsCollector(
        aws=FakeAwsContext(client, settings), notes=NoteCollector(), lookback_days=lookback_days
    )


def test_running_instances_get_metric_specs_but_stopped_ones_do_not():
    running = make_resource("i-run", state="running")
    stopped = make_resource("i-stop", state="stopped")

    outputs = {spec.output for spec in build_specs(running)}
    assert {"cpu_avg", "cpu_max", "cpu_p95", "network_in_bytes_per_day"} <= outputs
    assert build_specs(stopped) == []


def test_detached_volumes_are_not_queried():
    attached = make_resource("vol-1", resource_type="ebs:volume", state="in-use")
    detached = make_resource("vol-2", resource_type="ebs:volume", state="available")

    assert build_specs(attached)
    assert build_specs(detached) == []


def test_alb_dimension_is_the_tail_of_the_arn():
    arn = (
        "arn:aws:elasticloadbalancing:us-east-1:111122223333:"
        "loadbalancer/app/public-alb/50dc6c495c0c9188"
    )
    assert _alb_dimension(arn) == "app/public-alb/50dc6c495c0c9188"
    assert _alb_dimension("arn:aws:ec2:us-east-1:111122223333:instance/i-1") is None


def test_metrics_are_reduced_with_the_right_aggregation(settings):
    client = FakeCloudWatchClient(
        {
            "CPUUtilization": [10.0, 20.0, 30.0],
            "NetworkIn": [1000.0, 2000.0, 3000.0],
            "NetworkOut": [500.0, 500.0, 500.0],
        }
    )
    instance = make_resource("i-1", state="running")

    collector(client, settings).collect([instance])

    assert instance.metrics["cpu_avg"] == 20.0
    assert instance.metrics["cpu_max"] == 30.0
    # Daily sums averaged give bytes per day.
    assert instance.metrics["network_in_bytes_per_day"] == 2000.0
    assert instance.metrics["network_out_bytes_per_day"] == 500.0
    # Derived: in + out.
    assert instance.metrics["network_bytes_per_day"] == 2500.0


def test_last_aggregation_takes_the_most_recent_value(settings):
    # Values arrive newest-first because the scan is TimestampDescending.
    client = FakeCloudWatchClient({"BucketSizeBytes": [900.0, 800.0, 700.0]})
    bucket = make_resource("my-bucket", resource_type="s3:bucket", service="S3")

    collector(client, settings).collect([bucket])

    assert bucket.metrics["bucket_size_bytes"] == 900.0
    assert client.requests[0]["ScanBy"] == "TimestampDescending"


def test_lambda_invocations_are_scaled_to_a_month(settings):
    client = FakeCloudWatchClient({"Invocations": [100.0, 200.0], "Duration": [250.0]})
    function = make_resource(
        "api-handler",
        resource_type="lambda:function",
        service="Lambda",
        attributes={"memory_mb": 512},
    )

    collector(client, settings).collect([function])

    # Average of 150 invocations/day over a 30.44 day month.
    assert function.metrics["invocations_per_month"] == pytest.approx(150 * 30.44)
    assert function.metrics["duration_avg_ms"] == 250.0


def test_dynamodb_utilization_is_derived_from_provisioned_capacity(settings):
    # 8640 consumed units per day is 0.1 per second, or 1% of 10 provisioned.
    client = FakeCloudWatchClient(
        {"ConsumedReadCapacityUnits": [8640.0], "ConsumedWriteCapacityUnits": [8640.0]}
    )
    table = make_resource(
        "orders",
        resource_type="dynamodb:table",
        service="DynamoDB",
        attributes={
            "billing_mode": "PROVISIONED",
            "read_capacity_units": 10,
            "write_capacity_units": 100,
        },
    )

    collector(client, settings).collect([table])

    assert table.metrics["consumed_rcu_per_second"] == pytest.approx(0.1)
    assert table.metrics["read_utilization_percent"] == pytest.approx(1.0)
    assert table.metrics["write_utilization_percent"] == pytest.approx(0.1)


def test_on_demand_dynamodb_tables_are_not_queried(settings):
    client = FakeCloudWatchClient({"ConsumedReadCapacityUnits": [100.0]})
    table = make_resource(
        "events",
        resource_type="dynamodb:table",
        service="DynamoDB",
        attributes={"billing_mode": "PAY_PER_REQUEST"},
    )

    collector(client, settings).collect([table])

    assert table.metrics == {}
    assert client.requests == []


def test_nat_gateway_bytes_are_converted_to_monthly_gigabytes(settings):
    one_gb_per_day = float(1024**3)
    client = FakeCloudWatchClient({"BytesOutToDestination": [one_gb_per_day]})
    gateway = make_resource("nat-1", resource_type="ec2:nat-gateway", service="VPC")

    collector(client, settings).collect([gateway])

    assert gateway.metrics["nat_bytes_processed_per_month_gb"] == pytest.approx(30.44)


def test_queries_are_batched_to_the_api_limit(settings):
    client = FakeCloudWatchClient({"CPUUtilization": [5.0]})
    # Five specs per running instance, so 150 instances produce 750 queries.
    instances = [make_resource(f"i-{index}", state="running") for index in range(150)]

    collector(client, settings).collect(instances)

    assert client.query_count == 750
    assert len(client.requests) == 2
    assert len(client.requests[0]["MetricDataQueries"]) == MAX_QUERIES_PER_CALL
    assert len(client.requests[1]["MetricDataQueries"]) == 250


def test_empty_metric_series_leaves_the_field_absent(settings):
    client = FakeCloudWatchClient({})
    instance = make_resource("i-quiet", state="running")

    collector(client, settings).collect([instance])

    assert instance.metrics == {}


def test_denied_metric_access_is_recorded_and_does_not_raise(settings):
    client = FakeCloudWatchClient(fail=True)
    metrics = collector(client, settings)
    instance = make_resource("i-1", state="running")

    metrics.collect([instance])

    assert instance.metrics == {}
    note = next(n for n in metrics.notes.notes if n.capability == "cloudwatch:GetMetricData")
    assert note.status == "denied"
