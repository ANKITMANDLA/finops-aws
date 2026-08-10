"""DynamoDB tables: billing mode, provisioned capacity, table class, and backups."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from botocore.exceptions import BotoCoreError, ClientError

from finops.aws.collectors.base import (
    CollectionContext,
    Collector,
    paginate,
    register,
    tags_to_dict,
)
from finops.model import Resource

logger = logging.getLogger(__name__)


@register
class DynamoDbCollector(Collector):
    key = "dynamodb"
    service = "DynamoDB"

    def collect(self, ctx: CollectionContext, region: str) -> list[Resource]:
        client = ctx.client("dynamodb", region)
        table_names = list(paginate(client, "list_tables", "TableNames"))
        if not table_names:
            return []

        workers = min(ctx.settings.max_workers, max(len(table_names), 1))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ddb") as pool:
            results = list(
                pool.map(lambda name: self._describe(ctx, client, region, name), table_names)
            )
        return [resource for resource in results if resource is not None]

    def _describe(self, ctx: CollectionContext, client, region: str, name: str) -> Resource | None:
        try:
            table = client.describe_table(TableName=name)["Table"]
        except (ClientError, BotoCoreError) as exc:
            logger.debug("describe_table failed for %s: %s", name, exc)
            return None

        arn = table["TableArn"]
        throughput = table.get("ProvisionedThroughput", {}) or {}
        billing_mode = (table.get("BillingModeSummary") or {}).get(
            "BillingMode",
            "PROVISIONED" if throughput.get("ReadCapacityUnits") else "PAY_PER_REQUEST",
        )
        indexes = table.get("GlobalSecondaryIndexes", []) or []

        tags: dict[str, str] = {}
        try:
            tags = tags_to_dict(client.list_tags_of_resource(ResourceArn=arn).get("Tags"))
        except (ClientError, BotoCoreError) as exc:
            logger.debug("list_tags_of_resource failed for %s: %s", name, exc)

        pitr_enabled = None
        try:
            backups = client.describe_continuous_backups(TableName=name)
            pitr_enabled = (
                backups.get("ContinuousBackupsDescription", {})
                .get("PointInTimeRecoveryDescription", {})
                .get("PointInTimeRecoveryStatus")
                == "ENABLED"
            )
        except (ClientError, BotoCoreError) as exc:
            logger.debug("describe_continuous_backups failed for %s: %s", name, exc)

        return Resource(
            arn=arn,
            resource_id=name,
            resource_type="dynamodb:table",
            service="DynamoDB",
            region=region,
            account_id=ctx.account_id,
            name=name,
            state=table.get("TableStatus"),
            created_at=table.get("CreationDateTime"),
            tags=tags,
            attributes={
                "billing_mode": billing_mode,
                "read_capacity_units": throughput.get("ReadCapacityUnits"),
                "write_capacity_units": throughput.get("WriteCapacityUnits"),
                "table_class": (table.get("TableClassSummary") or {}).get("TableClass", "STANDARD"),
                "size_bytes": table.get("TableSizeBytes"),
                "item_count": table.get("ItemCount"),
                "global_secondary_index_count": len(indexes),
                "global_secondary_indexes": [
                    {
                        "name": index.get("IndexName"),
                        "read_capacity_units": (index.get("ProvisionedThroughput") or {}).get(
                            "ReadCapacityUnits"
                        ),
                        "write_capacity_units": (index.get("ProvisionedThroughput") or {}).get(
                            "WriteCapacityUnits"
                        ),
                        "size_bytes": index.get("IndexSizeBytes"),
                    }
                    for index in indexes
                ],
                "streams_enabled": (table.get("StreamSpecification") or {}).get(
                    "StreamEnabled", False
                ),
                "point_in_time_recovery": pitr_enabled,
                "global_table_regions": [
                    replica.get("RegionName") for replica in table.get("Replicas", []) or []
                ],
            },
        )
