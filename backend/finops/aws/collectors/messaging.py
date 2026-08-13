"""SNS topics, SQS queues, ECR repositories, and CloudWatch alarms.

The first two are here for completeness rather than for savings. Neither SNS nor SQS has a
standing charge: an idle topic and an idle queue cost exactly nothing, and the bill is
entirely per request. Both are collected so the inventory is honest about what exists, and
priced from measured request counts so a busy queue does not read as free.

ECR charges for stored image layers, and CloudWatch charges per alarm, both of which
accumulate quietly.
"""

from __future__ import annotations

import logging

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

BYTES_PER_GB = 1024**3

# Queue attributes worth reading. ApproximateNumberOfMessages says whether anything is
# waiting; the redrive policy says whether this queue is a dead letter target.
_QUEUE_ATTRIBUTES = (
    "QueueArn",
    "CreatedTimestamp",
    "FifoQueue",
    "ApproximateNumberOfMessages",
    "ApproximateNumberOfMessagesNotVisible",
    "MessageRetentionPeriod",
    "RedrivePolicy",
    "KmsMasterKeyId",
)


@register
class SnsTopicCollector(Collector):
    key = "sns"
    service = "SNS"

    def collect(self, ctx: CollectionContext, region: str) -> list[Resource]:
        client = ctx.client("sns", region)
        resources: list[Resource] = []
        for topic in paginate(client, "list_topics", "Topics"):
            arn = topic["TopicArn"]
            name = arn.rsplit(":", 1)[-1]
            attributes = self._attributes(client, arn)
            resources.append(
                Resource(
                    arn=arn,
                    resource_id=name,
                    resource_type="sns:topic",
                    service="SNS",
                    region=region,
                    account_id=ctx.account_id,
                    name=attributes.get("DisplayName") or name,
                    state="active",
                    tags=self._tags(client, arn),
                    attributes={
                        "fifo": attributes.get("FifoTopic") == "true",
                        # A topic nobody subscribes to publishes into nothing, which is
                        # usually a leftover rather than a design.
                        "subscription_count": _as_int(attributes.get("SubscriptionsConfirmed")),
                        "subscriptions_pending": _as_int(attributes.get("SubscriptionsPending")),
                        "kms_key_id": attributes.get("KmsMasterKeyId"),
                    },
                )
            )
        return resources

    def _attributes(self, client, arn: str) -> dict[str, str]:
        try:
            return client.get_topic_attributes(TopicArn=arn).get("Attributes", {})
        except (ClientError, BotoCoreError) as exc:
            logger.debug("get_topic_attributes failed for %s: %s", arn, exc)
            return {}

    def _tags(self, client, arn: str) -> dict[str, str]:
        try:
            return tags_to_dict(client.list_tags_for_resource(ResourceArn=arn).get("Tags"))
        except (ClientError, BotoCoreError) as exc:
            logger.debug("list_tags_for_resource failed for %s: %s", arn, exc)
            return {}


@register
class SqsQueueCollector(Collector):
    key = "sqs"
    service = "SQS"

    def collect(self, ctx: CollectionContext, region: str) -> list[Resource]:
        client = ctx.client("sqs", region)
        resources: list[Resource] = []
        for url in paginate(client, "list_queues", "QueueUrls"):
            name = url.rsplit("/", 1)[-1]
            attributes = self._attributes(client, url)
            arn = attributes.get("QueueArn") or f"arn:aws:sqs:{region}:{ctx.account_id}:{name}"
            resources.append(
                Resource(
                    arn=arn,
                    resource_id=name,
                    resource_type="sqs:queue",
                    service="SQS",
                    region=region,
                    account_id=ctx.account_id,
                    name=name,
                    state="active",
                    created_at=_epoch(attributes.get("CreatedTimestamp")),
                    tags=self._tags(client, url),
                    attributes={
                        "queue_url": url,
                        "fifo": attributes.get("FifoQueue") == "true",
                        "messages_available": _as_int(
                            attributes.get("ApproximateNumberOfMessages")
                        ),
                        "messages_in_flight": _as_int(
                            attributes.get("ApproximateNumberOfMessagesNotVisible")
                        ),
                        "retention_seconds": _as_int(attributes.get("MessageRetentionPeriod")),
                        "has_redrive_policy": bool(attributes.get("RedrivePolicy")),
                        "kms_key_id": attributes.get("KmsMasterKeyId"),
                    },
                )
            )
        return resources

    def _attributes(self, client, url: str) -> dict[str, str]:
        try:
            response = client.get_queue_attributes(
                QueueUrl=url, AttributeNames=list(_QUEUE_ATTRIBUTES)
            )
        except (ClientError, BotoCoreError) as exc:
            logger.debug("get_queue_attributes failed for %s: %s", url, exc)
            return {}
        return response.get("Attributes", {})

    def _tags(self, client, url: str) -> dict[str, str]:
        try:
            return client.list_queue_tags(QueueUrl=url).get("Tags", {}) or {}
        except (ClientError, BotoCoreError) as exc:
            logger.debug("list_queue_tags failed for %s: %s", url, exc)
            return {}


@register
class EcrRepositoryCollector(Collector):
    key = "ecr"
    service = "ECR"

    def collect(self, ctx: CollectionContext, region: str) -> list[Resource]:
        client = ctx.client("ecr", region)
        resources: list[Resource] = []
        for repository in paginate(client, "describe_repositories", "repositories"):
            name = repository["repositoryName"]
            size_bytes, image_count = self._size(client, name)
            size_gb = None if size_bytes is None else round(size_bytes / BYTES_PER_GB, 4)
            resources.append(
                Resource(
                    arn=repository["repositoryArn"],
                    resource_id=name,
                    resource_type="ecr:repository",
                    service="ECR",
                    region=region,
                    account_id=ctx.account_id,
                    name=name,
                    state="active",
                    created_at=repository.get("createdAt"),
                    tags=self._tags(client, repository["repositoryArn"]),
                    attributes={
                        "uri": repository.get("repositoryUri"),
                        "image_tag_mutability": repository.get("imageTagMutability"),
                        "scan_on_push": (repository.get("imageScanningConfiguration") or {}).get(
                            "scanOnPush"
                        ),
                        "image_count": image_count,
                        "size_bytes": size_bytes,
                        "size_gb": size_gb,
                        # Without a lifecycle policy nothing ever removes old images, and
                        # the repository grows for as long as the pipeline pushes to it.
                        "has_lifecycle_policy": self._has_lifecycle_policy(client, name),
                    },
                )
            )
        return resources

    def _size(self, client, name: str) -> tuple[int | None, int | None]:
        """Total stored bytes, summed over image versions.

        Layers shared between images are stored once but reported against each image that
        references them, so this is an upper bound rather than an exact figure. An
        unreadable repository returns no size at all rather than zero, so a denied call
        cannot be mistaken for an empty registry.
        """
        total = 0
        count = 0
        try:
            for image in paginate(client, "describe_images", "imageDetails", repositoryName=name):
                total += image.get("imageSizeInBytes") or 0
                count += 1
        except (ClientError, BotoCoreError) as exc:
            logger.debug("describe_images failed for %s: %s", name, exc)
            return None, None
        return total, count

    def _has_lifecycle_policy(self, client, name: str) -> bool | None:
        try:
            client.get_lifecycle_policy(repositoryName=name)
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "LifecyclePolicyNotFoundException":
                return False
            logger.debug("get_lifecycle_policy failed for %s: %s", name, exc)
            return None
        except BotoCoreError as exc:
            logger.debug("get_lifecycle_policy failed for %s: %s", name, exc)
            return None

    def _tags(self, client, arn: str) -> dict[str, str]:
        try:
            return tags_to_dict(client.list_tags_for_resource(resourceArn=arn).get("tags"))
        except (ClientError, BotoCoreError) as exc:
            logger.debug("list_tags_for_resource failed for %s: %s", arn, exc)
            return {}


@register
class CloudWatchAlarmCollector(Collector):
    key = "alarms"
    service = "CloudWatch"

    def collect(self, ctx: CollectionContext, region: str) -> list[Resource]:
        client = ctx.client("cloudwatch", region)
        resources: list[Resource] = []
        for alarm in paginate(client, "describe_alarms", "MetricAlarms"):
            resources.append(self._resource(ctx, region, alarm, composite=False))
        for alarm in paginate(client, "describe_alarms", "CompositeAlarms"):
            resources.append(self._resource(ctx, region, alarm, composite=True))
        return resources

    def _resource(
        self, ctx: CollectionContext, region: str, alarm: dict, *, composite: bool
    ) -> Resource:
        name = alarm["AlarmName"]
        period = alarm.get("Period")
        # A period under a minute makes this a high resolution alarm, at triple the price.
        kind = (
            "composite"
            if composite
            else ("high_resolution" if period and period < 60 else "standard")
        )
        return Resource(
            arn=alarm["AlarmArn"],
            resource_id=name,
            resource_type="cloudwatch:alarm",
            service="CloudWatch",
            region=region,
            account_id=ctx.account_id,
            name=name,
            state=alarm.get("StateValue"),
            created_at=alarm.get("AlarmConfigurationUpdatedTimestamp"),
            tags={},
            attributes={
                "alarm_kind": kind,
                "metric_name": alarm.get("MetricName"),
                "namespace": alarm.get("Namespace"),
                "period_seconds": period,
                "actions_enabled": alarm.get("ActionsEnabled", True),
                "action_count": len(alarm.get("AlarmActions") or []),
                # INSUFFICIENT_DATA for a whole scan window usually means the metric
                # stopped arriving, which usually means the resource is gone.
                "state_reason": alarm.get("StateReason"),
                "state_updated_at": (
                    alarm["StateUpdatedTimestamp"].isoformat()
                    if alarm.get("StateUpdatedTimestamp")
                    else None
                ),
            },
        )


def _as_int(raw: str | None) -> int | None:
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _epoch(raw: str | None):
    """SQS reports creation as a unix timestamp string."""
    from datetime import UTC, datetime

    value = _as_int(raw)
    return datetime.fromtimestamp(value, UTC) if value else None
