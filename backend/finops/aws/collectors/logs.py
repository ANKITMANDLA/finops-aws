"""CloudWatch log groups.

Log groups with no retention policy keep data forever at roughly $0.03/GB-month, which
compounds quietly for years. ``storedBytes`` is returned by the describe call, so the
cost of this data is known exactly without any extra API traffic.
"""

from __future__ import annotations

from finops.aws.collectors.base import CollectionContext, Collector, paginate, register
from finops.model import Resource
from finops.util import parse_aws_timestamp

BYTES_PER_GB = 1024**3


@register
class LogGroupCollector(Collector):
    key = "logs"
    service = "CloudWatch Logs"

    def collect(self, ctx: CollectionContext, region: str) -> list[Resource]:
        client = ctx.client("logs", region)
        resources: list[Resource] = []
        for group in paginate(client, "describe_log_groups", "logGroups"):
            name = group["logGroupName"]
            stored_bytes = group.get("storedBytes") or 0
            retention_days = group.get("retentionInDays")
            resources.append(
                Resource(
                    arn=group.get("arn") or group.get("logGroupArn") or name,
                    resource_id=name,
                    resource_type="logs:log-group",
                    service="CloudWatch Logs",
                    region=region,
                    account_id=ctx.account_id,
                    name=name,
                    state="active",
                    created_at=parse_aws_timestamp(group.get("creationTime", 0) / 1000)
                    if group.get("creationTime")
                    else None,
                    attributes={
                        "retention_days": retention_days,
                        "never_expires": retention_days is None,
                        "stored_bytes": stored_bytes,
                        "stored_gb": round(stored_bytes / BYTES_PER_GB, 4),
                        "log_group_class": group.get("logGroupClass", "STANDARD"),
                        "metric_filter_count": group.get("metricFilterCount", 0),
                        "kms_key_id": group.get("kmsKeyId"),
                    },
                )
            )
        return resources
