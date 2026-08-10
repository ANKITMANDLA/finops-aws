"""Lambda functions, including provisioned concurrency, which bills whether used or not."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from finops.aws.collectors.base import CollectionContext, Collector, paginate, register
from finops.model import Resource
from finops.util import parse_aws_timestamp

logger = logging.getLogger(__name__)


@register
class LambdaCollector(Collector):
    key = "lambda"
    service = "Lambda"

    def collect(self, ctx: CollectionContext, region: str) -> list[Resource]:
        client = ctx.client("lambda", region)
        functions = list(paginate(client, "list_functions", "Functions"))
        if not functions:
            return []

        workers = min(ctx.settings.max_workers, max(len(functions), 1))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="lambda") as pool:
            details = list(pool.map(lambda f: self._detail(client, f), functions))

        resources: list[Resource] = []
        for function, extra in zip(functions, details, strict=True):
            arn = function["FunctionArn"]
            tags = extra["tags"]
            resources.append(
                Resource(
                    arn=arn,
                    resource_id=function["FunctionName"],
                    resource_type="lambda:function",
                    service="Lambda",
                    region=region,
                    account_id=ctx.account_id,
                    name=function["FunctionName"],
                    state=function.get("State", "Active"),
                    created_at=parse_aws_timestamp(function.get("LastModified")),
                    tags=tags,
                    attributes={
                        "runtime": function.get("Runtime") or function.get("PackageType"),
                        "package_type": function.get("PackageType", "Zip"),
                        "memory_mb": function.get("MemorySize"),
                        "timeout_seconds": function.get("Timeout"),
                        "architectures": function.get("Architectures", ["x86_64"]),
                        "code_size_bytes": function.get("CodeSize"),
                        "ephemeral_storage_mb": function.get("EphemeralStorage", {}).get("Size"),
                        "reserved_concurrency": extra["reserved_concurrency"],
                        "provisioned_concurrency": extra["provisioned_concurrency"],
                        "in_vpc": bool(function.get("VpcConfig", {}).get("VpcId")),
                        "last_modified": function.get("LastModified"),
                        "layers": [layer.get("Arn") for layer in function.get("Layers", [])],
                    },
                )
            )
        return resources

    def _detail(self, client, function: dict[str, Any]) -> dict[str, Any]:
        arn = function["FunctionArn"]
        name = function["FunctionName"]
        tags: dict[str, str] = {}
        try:
            tags = dict(client.list_tags(Resource=arn).get("Tags", {}) or {})
        except (ClientError, BotoCoreError) as exc:
            logger.debug("list_tags failed for %s: %s", name, exc)

        provisioned = 0
        try:
            for config in paginate(
                client,
                "list_provisioned_concurrency_configs",
                "ProvisionedConcurrencyConfigs",
                FunctionName=name,
            ):
                provisioned += config.get("AllocatedProvisionedConcurrentExecutions") or 0
        except (ClientError, BotoCoreError) as exc:
            logger.debug("list_provisioned_concurrency_configs failed for %s: %s", name, exc)

        reserved = None
        try:
            reserved = client.get_function_concurrency(FunctionName=name).get(
                "ReservedConcurrentExecutions"
            )
        except (ClientError, BotoCoreError) as exc:
            logger.debug("get_function_concurrency failed for %s: %s", name, exc)

        return {
            "tags": tags,
            "provisioned_concurrency": provisioned,
            "reserved_concurrency": reserved,
        }
