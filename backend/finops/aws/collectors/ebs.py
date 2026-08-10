"""EBS volumes, snapshots, and self-owned AMIs."""

from __future__ import annotations

from finops.aws.collectors.base import (
    CollectionContext,
    Collector,
    paginate,
    register,
    synthesize_arn,
    tags_to_dict,
)
from finops.model import Resource
from finops.util import parse_aws_timestamp


@register
class EbsVolumeCollector(Collector):
    key = "ebs"
    service = "EBS"

    def collect(self, ctx: CollectionContext, region: str) -> list[Resource]:
        client = ctx.client("ec2", region)
        resources: list[Resource] = []
        for volume in paginate(client, "describe_volumes", "Volumes"):
            tags = tags_to_dict(volume.get("Tags"))
            attachments = volume.get("Attachments", [])
            attachment = attachments[0] if attachments else {}
            volume_id = volume["VolumeId"]
            resources.append(
                Resource(
                    arn=synthesize_arn("ec2", region, ctx.account_id, f"volume/{volume_id}"),
                    resource_id=volume_id,
                    resource_type="ebs:volume",
                    service="EBS",
                    region=region,
                    account_id=ctx.account_id,
                    name=tags.get("Name"),
                    availability_zone=volume.get("AvailabilityZone"),
                    state=volume.get("State"),  # available == unattached
                    created_at=volume.get("CreateTime"),
                    tags=tags,
                    attributes={
                        "volume_type": volume.get("VolumeType"),
                        "size_gb": volume.get("Size"),
                        "iops": volume.get("Iops"),
                        "throughput_mibps": volume.get("Throughput"),
                        "encrypted": volume.get("Encrypted", False),
                        "multi_attach": volume.get("MultiAttachEnabled", False),
                        "source_snapshot_id": volume.get("SnapshotId") or None,
                        "attached_instance_id": attachment.get("InstanceId"),
                        "attachment_state": attachment.get("State"),
                        "delete_on_termination": attachment.get("DeleteOnTermination"),
                        "device": attachment.get("Device"),
                        "attachment_count": len(attachments),
                    },
                )
            )
        return resources


@register
class EbsSnapshotCollector(Collector):
    key = "ebs-snapshot"
    service = "EBS Snapshots"

    def collect(self, ctx: CollectionContext, region: str) -> list[Resource]:
        client = ctx.client("ec2", region)
        resources: list[Resource] = []
        # OwnerIds=self keeps us from enumerating every public snapshot in the region.
        for snapshot in paginate(client, "describe_snapshots", "Snapshots", OwnerIds=["self"]):
            tags = tags_to_dict(snapshot.get("Tags"))
            snapshot_id = snapshot["SnapshotId"]
            resources.append(
                Resource(
                    arn=synthesize_arn("ec2", region, ctx.account_id, f"snapshot/{snapshot_id}"),
                    resource_id=snapshot_id,
                    resource_type="ebs:snapshot",
                    service="EBS Snapshots",
                    region=region,
                    account_id=ctx.account_id,
                    name=tags.get("Name"),
                    state=snapshot.get("State"),
                    created_at=snapshot.get("StartTime"),
                    tags=tags,
                    attributes={
                        "volume_id": snapshot.get("VolumeId"),
                        "volume_size_gb": snapshot.get("VolumeSize"),
                        "storage_tier": snapshot.get("StorageTier", "standard"),
                        "description": (snapshot.get("Description") or "")[:400],
                        "encrypted": snapshot.get("Encrypted", False),
                        # Set when the snapshot belongs to an AMI or a backup plan.
                        "owner_alias": snapshot.get("OwnerAlias"),
                        "full_snapshot_size_bytes": snapshot.get("FullSnapshotSizeInBytes"),
                    },
                )
            )
        return resources


@register
class AmiCollector(Collector):
    key = "ami"
    service = "AMI"

    def collect(self, ctx: CollectionContext, region: str) -> list[Resource]:
        client = ctx.client("ec2", region)
        resources: list[Resource] = []
        for image in paginate(client, "describe_images", "Images", Owners=["self"]):
            tags = tags_to_dict(image.get("Tags"))
            image_id = image["ImageId"]
            snapshot_ids: list[str] = []
            backing_size_gb = 0
            for mapping in image.get("BlockDeviceMappings", []):
                ebs = mapping.get("Ebs") or {}
                if ebs.get("SnapshotId"):
                    snapshot_ids.append(ebs["SnapshotId"])
                backing_size_gb += ebs.get("VolumeSize") or 0
            resources.append(
                Resource(
                    arn=synthesize_arn("ec2", region, ctx.account_id, f"image/{image_id}"),
                    resource_id=image_id,
                    resource_type="ec2:image",
                    service="AMI",
                    region=region,
                    account_id=ctx.account_id,
                    name=image.get("Name") or tags.get("Name"),
                    state=image.get("State"),
                    created_at=parse_aws_timestamp(image.get("CreationDate")),
                    tags=tags,
                    attributes={
                        "snapshot_ids": snapshot_ids,
                        "backing_size_gb": backing_size_gb,
                        "architecture": image.get("Architecture"),
                        "platform": image.get("Platform") or "linux",
                        "public": image.get("Public", False),
                        "deprecation_time": image.get("DeprecationTime"),
                        "description": (image.get("Description") or "")[:400],
                    },
                )
            )
        return resources
