"""Amazon EFS file systems.

Two things drive an EFS bill, and both are visible without touching the data. Storage is
metered per tier, and ``DescribeFileSystems`` already reports how many bytes sit in
Standard, Infrequent Access, and Archive, so the storage line can be priced exactly rather
than estimated. Provisioned throughput is the other half, and it is easy to forget about:
it is configured once, billed per MiB/s every hour after that, and invisible in the
console's file system list.

The lifecycle configuration is fetched per file system because it decides whether cold
data ever reaches the cheaper tiers.
"""

from __future__ import annotations

import logging
from typing import Any

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

# Lifecycle transitions, keyed by the attribute each one is reported under.
_TRANSITIONS = (
    ("TransitionToIA", "transition_to_ia"),
    ("TransitionToArchive", "transition_to_archive"),
    ("TransitionToPrimaryStorageClass", "transition_to_primary_storage_class"),
)


@register
class EfsFileSystemCollector(Collector):
    key = "efs"
    service = "EFS"

    def collect(self, ctx: CollectionContext, region: str) -> list[Resource]:
        client = ctx.client("efs", region)
        resources: list[Resource] = []
        for file_system in paginate(client, "describe_file_systems", "FileSystems"):
            file_system_id = file_system["FileSystemId"]
            tags = tags_to_dict(file_system.get("Tags"))
            size = file_system.get("SizeInBytes") or {}
            # One Zone file systems name their single AZ; regional ones leave it unset.
            availability_zone = file_system.get("AvailabilityZoneName")
            resources.append(
                Resource(
                    arn=file_system.get("FileSystemArn")
                    or f"arn:aws:elasticfilesystem:{region}:{ctx.account_id}"
                    f":file-system/{file_system_id}",
                    resource_id=file_system_id,
                    resource_type="efs:file-system",
                    service="EFS",
                    region=region,
                    account_id=ctx.account_id,
                    name=file_system.get("Name") or tags.get("Name"),
                    availability_zone=availability_zone,
                    state=file_system.get("LifeCycleState"),
                    created_at=file_system.get("CreationTime"),
                    tags=tags,
                    attributes={
                        **_sizes(size),
                        "one_zone": bool(availability_zone),
                        "performance_mode": file_system.get("PerformanceMode"),
                        "throughput_mode": file_system.get("ThroughputMode"),
                        "provisioned_throughput_mibps": file_system.get(
                            "ProvisionedThroughputInMibps"
                        ),
                        "mount_target_count": file_system.get("NumberOfMountTargets", 0),
                        "encrypted": file_system.get("Encrypted", False),
                        "kms_key_id": file_system.get("KmsKeyId"),
                        # DISABLED marks the destination of a replication pair, which is
                        # written to by AWS rather than by any client of its own.
                        "replication_overwrite_protection": (
                            file_system.get("FileSystemProtection") or {}
                        ).get("ReplicationOverwriteProtection"),
                        **self._lifecycle(client, file_system_id),
                    },
                )
            )
        return resources

    def _lifecycle(self, client, file_system_id: str) -> dict[str, Any]:
        """Which tiers cold data is allowed to move to, and after how long."""
        try:
            policies = client.describe_lifecycle_configuration(FileSystemId=file_system_id).get(
                "LifecyclePolicies", []
            )
        except (ClientError, BotoCoreError) as exc:
            logger.debug("describe_lifecycle_configuration failed for %s: %s", file_system_id, exc)
            return {"has_lifecycle_policy": None}

        # Each transition arrives as its own single-key dict.
        merged = {key: value for policy in policies for key, value in policy.items()}
        attributes: dict[str, Any] = {
            attribute: merged.get(field) for field, attribute in _TRANSITIONS
        }
        attributes["has_lifecycle_policy"] = bool(policies)
        return attributes


def _sizes(size: dict[str, Any]) -> dict[str, Any]:
    """Bytes per storage tier, as EFS last metered them, in both bytes and GB."""
    total = size.get("Value") or 0
    standard = size.get("ValueInStandard")
    infrequent = size.get("ValueInIA") or 0
    archive = size.get("ValueInArchive") or 0
    # Older file systems report only the total; everything in it is Standard.
    if standard is None:
        standard = max(total - infrequent - archive, 0)
    measured_at = size.get("Timestamp")
    return {
        "size_bytes": total,
        "standard_bytes": standard,
        "ia_bytes": infrequent,
        "archive_bytes": archive,
        "size_gb": round(total / BYTES_PER_GB, 4),
        "standard_gb": round(standard / BYTES_PER_GB, 4),
        "ia_gb": round(infrequent / BYTES_PER_GB, 4),
        "archive_gb": round(archive / BYTES_PER_GB, 4),
        "size_measured_at": measured_at.isoformat() if measured_at else None,
    }
