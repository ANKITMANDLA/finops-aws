"""S3 buckets and the configuration that drives their storage bill.

Bucket size comes from CloudWatch daily storage metrics rather than from listing
objects, which would be prohibitively slow and expensive on real accounts. What we
gather here is the configuration that decides whether that stored data is priced well:
lifecycle rules, Intelligent-Tiering, versioning, and abandoned multipart uploads.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from finops.aws.collectors.base import (
    CollectionContext,
    Collector,
    register,
    tags_to_dict,
)
from finops.model import Resource
from finops.util import parse_aws_timestamp

logger = logging.getLogger(__name__)

# Buckets created before regional endpoints report these legacy location constraints.
_LEGACY_LOCATIONS = {None: "us-east-1", "": "us-east-1", "EU": "eu-west-1"}


@register
class S3BucketCollector(Collector):
    key = "s3"
    service = "S3"
    scope = "global"

    def collect(self, ctx: CollectionContext, region: str) -> list[Resource]:
        control = ctx.client("s3", region)
        buckets = control.list_buckets().get("Buckets", [])
        if not buckets:
            return []

        workers = min(ctx.settings.max_workers, max(len(buckets), 1))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="s3") as pool:
            results = list(pool.map(lambda b: self._describe(ctx, control, b), buckets))
        return [resource for resource in results if resource is not None]

    def _describe(self, ctx: CollectionContext, control, bucket: dict[str, Any]) -> Resource | None:
        name = bucket["Name"]
        try:
            location = control.get_bucket_location(Bucket=name).get("LocationConstraint")
        except (ClientError, BotoCoreError) as exc:
            logger.debug("get_bucket_location failed for %s: %s", name, exc)
            return None
        bucket_region = _LEGACY_LOCATIONS.get(location, location) or "us-east-1"
        if not ctx.in_scope(bucket_region):
            return None

        client = ctx.client("s3", bucket_region)
        tags = self._tags(client, name)
        lifecycle_rules = self._lifecycle_rules(client, name)
        return Resource(
            arn=f"arn:aws:s3:::{name}",
            resource_id=name,
            resource_type="s3:bucket",
            service="S3",
            region=bucket_region,
            account_id=ctx.account_id,
            name=name,
            state="active",
            created_at=parse_aws_timestamp(bucket.get("CreationDate")),
            tags=tags,
            attributes={
                "versioning": self._versioning(client, name),
                "lifecycle_rule_count": len(lifecycle_rules),
                "has_lifecycle": bool(lifecycle_rules),
                "has_expiration_rule": any(
                    "Expiration" in rule or "NoncurrentVersionExpiration" in rule
                    for rule in lifecycle_rules
                ),
                "has_transition_rule": any(
                    "Transitions" in rule or "NoncurrentVersionTransitions" in rule
                    for rule in lifecycle_rules
                ),
                "has_abort_incomplete_upload_rule": any(
                    "AbortIncompleteMultipartUpload" in rule for rule in lifecycle_rules
                ),
                "intelligent_tiering_configs": self._intelligent_tiering(client, name),
                **self._incomplete_uploads(client, name),
            },
        )

    def _tags(self, client, name: str) -> dict[str, str]:
        try:
            return tags_to_dict(client.get_bucket_tagging(Bucket=name).get("TagSet"))
        except (ClientError, BotoCoreError):
            # NoSuchTagSet is the normal response for an untagged bucket.
            return {}

    def _versioning(self, client, name: str) -> str:
        try:
            return client.get_bucket_versioning(Bucket=name).get("Status", "Disabled")
        except (ClientError, BotoCoreError):
            return "Unknown"

    def _lifecycle_rules(self, client, name: str) -> list[dict[str, Any]]:
        try:
            return client.get_bucket_lifecycle_configuration(Bucket=name).get("Rules", [])
        except (ClientError, BotoCoreError):
            # NoSuchLifecycleConfiguration simply means no rules exist.
            return []

    def _intelligent_tiering(self, client, name: str) -> int:
        try:
            response = client.list_bucket_intelligent_tiering_configurations(Bucket=name)
            return len(response.get("IntelligentTieringConfigurationList", []))
        except (ClientError, BotoCoreError):
            return 0

    def _incomplete_uploads(self, client, name: str) -> dict[str, Any]:
        """Abandoned multipart uploads keep billing for storage nobody can see."""
        try:
            response = client.list_multipart_uploads(Bucket=name, MaxUploads=1000)
        except (ClientError, BotoCoreError):
            return {"incomplete_multipart_uploads": None, "oldest_incomplete_upload": None}
        uploads = response.get("Uploads", []) or []
        initiated = [parse_aws_timestamp(u.get("Initiated")) for u in uploads]
        oldest = min((d for d in initiated if d), default=None)
        return {
            "incomplete_multipart_uploads": len(uploads),
            "oldest_incomplete_upload": oldest.isoformat() if oldest else None,
        }
