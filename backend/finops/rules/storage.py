"""Storage that is unattached, over-specified, or never expires."""

from __future__ import annotations

from collections.abc import Iterable

from finops.model import (
    ACTION_DELETE,
    ACTION_MODIFY_STORAGE,
    ACTION_SET_RETENTION,
    Evidence,
    Finding,
    Remediation,
)
from finops.rules.base import Rule, RuleContext, finding_for, register
from finops.util import human_money

# Below this size, storage tiering work costs more in engineering time than it saves.
MIN_INTERESTING_GB = 50

# Typical saving from moving cold objects to a cheaper class or expiring them. Applied
# only as a clearly-labelled heuristic to bucket storage cost.
LIFECYCLE_SAVINGS_FRACTION = 0.30


@register
class UnattachedEbsVolume(Rule):
    """An available volume is attached to nothing and bills at full rate."""

    id = "ebs.unattached_volume"
    category = "storage"
    title = "Unattached EBS volume"

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        for volume in ctx.of_type("ebs:volume"):
            if volume.state != "available":
                continue
            age = ctx.age_days(volume)
            # A volume detached minutes ago is probably mid-migration, not abandoned.
            if age is not None and age < ctx.thresholds.ebs_unattached_min_age_days:
                continue

            savings = ctx.monthly_cost(volume)
            size_gb = volume.attributes.get("size_gb")
            volume_type = volume.attributes.get("volume_type")
            yield finding_for(
                volume,
                rule_id=self.id,
                title=f"Unattached {size_gb} GB {volume_type} volume {volume.resource_id}",
                category=self.category,
                action=ACTION_DELETE,
                savings=savings,
                detail=(
                    f"This volume has been in the available state for {age:.0f} days if the "
                    "creation date is any guide, meaning it is attached to no instance while "
                    "billing the full per-GB rate."
                    if age is not None
                    else "This volume is attached to no instance but bills the full per-GB rate."
                ),
                evidence=[
                    Evidence(label="State", value="available (unattached)"),
                    Evidence(label="Size", value=f"{size_gb} GB"),
                    Evidence(label="Type", value=str(volume_type)),
                    Evidence(
                        label="Age", value=f"{age:.0f} days" if age is not None else "unknown"
                    ),
                    Evidence(
                        label="Created from snapshot",
                        value=str(volume.attributes.get("source_snapshot_id") or "no"),
                    ),
                ],
                remediation=Remediation(
                    summary=(
                        "Snapshot the volume if the data may be needed, then delete it. A "
                        "snapshot costs a fraction of a live volume."
                    ),
                    cli=(
                        f"aws ec2 create-snapshot --volume-id {volume.resource_id} "
                        f"--description 'pre-delete backup' --region {volume.region} && "
                        f"aws ec2 delete-volume --volume-id {volume.resource_id} "
                        f"--region {volume.region}"
                    ),
                    console_path="EC2 > Volumes",
                ),
                confidence="high",
                effort="low",
                risk="medium",
                rollback_possible=False,
            )


@register
class Gp2ToGp3Migration(Rule):
    """gp3 is cheaper per GB than gp2 and can be changed with no downtime."""

    id = "ebs.gp2_to_gp3"
    category = "storage"
    title = "Migrate gp2 volumes to gp3"

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        for volume in ctx.of_type("ebs:volume"):
            if volume.attributes.get("volume_type") != "gp2":
                continue
            size_gb = volume.attributes.get("size_gb") or 0
            if not size_gb:
                continue

            gp2_price = ctx.pricing.ebs_gb_month(volume.region, "gp2")
            gp3_price = ctx.pricing.ebs_gb_month(volume.region, "gp3")
            if gp2_price is None or gp3_price is None:
                continue
            savings = (gp2_price.amount - gp3_price.amount) * size_gb
            if savings <= 0:
                continue

            yield finding_for(
                volume,
                rule_id=self.id,
                title=f"Convert {size_gb} GB volume {volume.resource_id} from gp2 to gp3",
                category=self.category,
                action=ACTION_MODIFY_STORAGE,
                savings=savings,
                detail=(
                    "gp3 costs less per GB than gp2 and delivers a 3,000 IOPS and 125 MiB/s "
                    "baseline regardless of size. The change is an online modification with no "
                    "downtime and no data movement, which makes this one of the safest savings "
                    "available."
                ),
                evidence=[
                    Evidence(label="Current type", value="gp2"),
                    Evidence(label="Recommended type", value="gp3"),
                    Evidence(label="Size", value=f"{size_gb} GB"),
                    Evidence(label="gp2 rate", value=f"${gp2_price.amount:.3f}/GB-month"),
                    Evidence(label="gp3 rate", value=f"${gp3_price.amount:.3f}/GB-month"),
                    Evidence(label="Downtime required", value="none"),
                ],
                remediation=Remediation(
                    summary="Modify the volume type in place. No detach or restart is needed.",
                    cli=(
                        f"aws ec2 modify-volume --volume-id {volume.resource_id} "
                        f"--volume-type gp3 --region {volume.region}"
                    ),
                    terraform='# In aws_ebs_volume / root_block_device: type = "gp3"',
                    console_path="EC2 > Volumes > Modify volume",
                ),
                confidence="high",
                effort="low",
                risk="low",
                cost_basis="list_price_estimate",
            )


@register
class OverProvisionedVolumeIops(Rule):
    """Provisioned IOPS that observed traffic never comes close to using."""

    id = "ebs.overprovisioned_iops"
    category = "storage"
    title = "Over-provisioned EBS IOPS"

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        for volume in ctx.of_type("ebs:volume"):
            volume_type = volume.attributes.get("volume_type")
            if volume_type not in {"io1", "io2", "gp3"}:
                continue
            provisioned = volume.attributes.get("iops") or 0
            observed = volume.metrics.get("volume_iops_observed")
            if not provisioned or observed is None:
                continue
            # gp3's first 3,000 IOPS are free, so only the paid excess is interesting.
            billable = provisioned - 3000 if volume_type == "gp3" else provisioned
            if billable <= 0:
                continue
            if observed > provisioned * ctx.thresholds.ebs_iops_overprovisioned_ratio:
                continue

            iops_price = ctx.pricing.ebs_iops_month(volume.region, volume_type)
            if iops_price is None:
                continue
            # Leave generous headroom: target double the observed peak, floor at the free tier.
            target = max(int(observed * 2), 3000 if volume_type == "gp3" else 100)
            reducible = max(provisioned - target, 0)
            savings = reducible * iops_price.amount
            if savings <= 0:
                continue

            yield finding_for(
                volume,
                rule_id=self.id,
                title=(
                    f"Volume {volume.resource_id} provisions {provisioned:,} IOPS but uses "
                    f"about {observed:,.0f}"
                ),
                category=self.category,
                action=ACTION_MODIFY_STORAGE,
                savings=savings,
                detail=(
                    f"Observed average throughput is {observed:,.0f} IOPS against "
                    f"{provisioned:,} provisioned. Reducing to {target:,} IOPS still leaves "
                    "roughly double the measured demand as headroom."
                ),
                evidence=[
                    Evidence(label="Provisioned IOPS", value=f"{provisioned:,}"),
                    Evidence(label="Observed IOPS", value=f"{observed:,.0f}"),
                    Evidence(label="Suggested IOPS", value=f"{target:,}"),
                    Evidence(label="Volume type", value=str(volume_type)),
                    Evidence(label="Rate", value=f"${iops_price.amount:.4f}/IOPS-month"),
                ],
                remediation=Remediation(
                    summary="Reduce provisioned IOPS with an online volume modification.",
                    cli=(
                        f"aws ec2 modify-volume --volume-id {volume.resource_id} "
                        f"--iops {target} --region {volume.region}"
                    ),
                    console_path="EC2 > Volumes > Modify volume",
                ),
                confidence="medium",
                effort="low",
                risk="medium",
                cost_basis="list_price_estimate",
            )


@register
class StaleSnapshot(Rule):
    """Old snapshots whose source volume is gone and which back no AMI."""

    id = "ebs.stale_snapshot"
    category = "storage"
    title = "Stale EBS snapshot"

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        present_volumes = ctx.volume_ids_present
        backing_images = ctx.snapshot_ids_backing_images

        for snapshot in ctx.of_type("ebs:snapshot"):
            # Deleting a snapshot an AMI depends on would break the image.
            if snapshot.resource_id in backing_images:
                continue
            age = ctx.age_days(snapshot)
            if age is None or age < ctx.thresholds.snapshot_stale_age_days:
                continue
            source_volume = snapshot.attributes.get("volume_id")
            if source_volume and source_volume in present_volumes:
                continue

            savings = ctx.monthly_cost(snapshot)
            yield finding_for(
                snapshot,
                rule_id=self.id,
                title=f"Snapshot {snapshot.resource_id} is {age:.0f} days old and orphaned",
                category=self.category,
                action=ACTION_DELETE,
                savings=savings,
                detail=(
                    f"The volume this snapshot came from ({source_volume or 'unknown'}) no longer "
                    f"exists, no AMI references the snapshot, and it is {age:.0f} days old. "
                    "Unless it is a deliberate long-term archive, it is paying storage for "
                    "history nobody will restore."
                ),
                evidence=[
                    Evidence(label="Age", value=f"{age:.0f} days"),
                    Evidence(label="Source volume", value=str(source_volume or "unknown")),
                    Evidence(label="Source volume still exists", value="no"),
                    Evidence(label="Size", value=f"{snapshot.attributes.get('volume_size_gb')} GB"),
                    Evidence(
                        label="Storage tier",
                        value=str(snapshot.attributes.get("storage_tier", "standard")),
                    ),
                    Evidence(
                        label="Description",
                        value=str(snapshot.attributes.get("description") or "none"),
                    ),
                ],
                remediation=Remediation(
                    summary=(
                        "Delete the snapshot, or move it to the archive tier if it must be "
                        "retained for compliance."
                    ),
                    cli=(
                        f"aws ec2 delete-snapshot --snapshot-id {snapshot.resource_id} "
                        f"--region {snapshot.region}"
                    ),
                    console_path="EC2 > Snapshots",
                ),
                confidence="medium",
                effort="low",
                risk="medium",
                rollback_possible=False,
            )


@register
class UnusedAmi(Rule):
    """A self-owned AMI no instance was launched from, plus its backing snapshots."""

    id = "ami.unused"
    category = "storage"
    title = "Unused AMI"

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        in_use = ctx.image_ids_in_use
        snapshots_by_id = {s.resource_id: s for s in ctx.of_type("ebs:snapshot")}

        for image in ctx.of_type("ec2:image"):
            if image.resource_id in in_use:
                continue
            age = ctx.age_days(image)
            if age is None or age < ctx.thresholds.ami_stale_age_days:
                continue

            snapshot_ids = image.attributes.get("snapshot_ids") or []
            # The AMI itself is free; the saving is the snapshots it holds open.
            savings = sum(
                ctx.monthly_cost(snapshots_by_id[sid])
                for sid in snapshot_ids
                if sid in snapshots_by_id
            )
            if savings <= 0:
                continue

            yield finding_for(
                image,
                rule_id=self.id,
                title=f"AMI {image.display_name} is unused and {age:.0f} days old",
                category=self.category,
                action=ACTION_DELETE,
                savings=savings,
                detail=(
                    f"No running or stopped instance was launched from this AMI, and it is "
                    f"{age:.0f} days old. The image costs nothing itself, but it holds "
                    f"{len(snapshot_ids)} snapshot(s) that cannot be deleted while it exists."
                ),
                evidence=[
                    Evidence(label="Age", value=f"{age:.0f} days"),
                    Evidence(label="Instances using it", value="0"),
                    Evidence(label="Backing snapshots", value=str(len(snapshot_ids))),
                    Evidence(
                        label="Backing size",
                        value=f"{image.attributes.get('backing_size_gb')} GB",
                    ),
                    Evidence(label="Snapshot cost", value=f"{human_money(savings)}/month"),
                ],
                remediation=Remediation(
                    summary="Deregister the AMI, then delete the snapshots it released.",
                    cli=(
                        f"aws ec2 deregister-image --image-id {image.resource_id} "
                        f"--region {image.region}\n"
                        + "\n".join(
                            f"aws ec2 delete-snapshot --snapshot-id {sid} --region {image.region}"
                            for sid in snapshot_ids
                        )
                    ),
                    console_path="EC2 > AMIs",
                ),
                confidence="medium",
                effort="low",
                risk="medium",
                rollback_possible=False,
            )


@register
class LogGroupWithoutRetention(Rule):
    """Log groups default to keeping data forever."""

    id = "logs.no_retention"
    category = "storage"
    title = "Log group that never expires"

    # Assume a 30-day retention policy would shed this share of stored data. Deliberately
    # conservative given we cannot see the age distribution of the log events.
    RETENTION_SAVINGS_FRACTION = 0.70

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        for group in ctx.of_type("logs:log-group"):
            retention = group.attributes.get("retention_days")
            stored_gb = group.attributes.get("stored_gb") or 0
            if stored_gb < 1:
                continue
            never_expires = retention is None
            too_long = (
                retention is not None and retention > ctx.thresholds.log_group_retention_max_days
            )
            if not (never_expires or too_long):
                continue

            current_cost = ctx.monthly_cost(group)
            savings = current_cost * self.RETENTION_SAVINGS_FRACTION
            if savings <= 0:
                continue

            policy = "never expires" if never_expires else f"{retention} days"
            yield finding_for(
                group,
                rule_id=self.id,
                title=f"Log group {group.resource_id} holds {stored_gb:,.1f} GB and {policy}",
                category=self.category,
                action=ACTION_SET_RETENTION,
                savings=savings,
                detail=(
                    f"The group stores {stored_gb:,.1f} GB with a retention policy of {policy}. "
                    "Storage bills per GB-month indefinitely. Setting a retention period is a "
                    "single API call and applies to existing data, though expiry is permanent."
                ),
                evidence=[
                    Evidence(label="Retention policy", value=policy),
                    Evidence(label="Stored data", value=f"{stored_gb:,.2f} GB"),
                    Evidence(label="Current cost", value=f"{human_money(current_cost)}/month"),
                    Evidence(
                        label="Log class",
                        value=str(group.attributes.get("log_group_class", "STANDARD")),
                    ),
                    Evidence(
                        label="Estimate basis",
                        value=f"assumes {self.RETENTION_SAVINGS_FRACTION:.0%} of data is older "
                        "than 30 days",
                    ),
                ],
                remediation=Remediation(
                    summary=(
                        "Set a retention period that matches how far back anyone actually reads. "
                        "Export to S3 first if the logs are needed for compliance."
                    ),
                    cli=(
                        "aws logs put-retention-policy --log-group-name "
                        f"'{group.resource_id}' --retention-in-days 30 --region {group.region}"
                    ),
                    terraform="# aws_cloudwatch_log_group: retention_in_days = 30",
                    console_path="CloudWatch > Log groups",
                ),
                confidence="medium",
                effort="low",
                risk="medium",
                cost_basis="heuristic",
                rollback_possible=False,
            )


@register
class BucketWithoutLifecyclePolicy(Rule):
    """S3 data with no lifecycle rule stays in Standard forever."""

    id = "s3.no_lifecycle"
    category = "storage"
    title = "S3 bucket without a lifecycle policy"

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        for bucket in ctx.of_type("s3:bucket"):
            attributes = bucket.attributes
            if attributes.get("has_transition_rule") or attributes.get(
                "intelligent_tiering_configs"
            ):
                continue
            size_bytes = bucket.metrics.get("bucket_size_bytes") or 0
            size_gb = size_bytes / 1024**3
            if size_gb < MIN_INTERESTING_GB:
                continue

            current_cost = ctx.monthly_cost(bucket)
            savings = current_cost * LIFECYCLE_SAVINGS_FRACTION
            if savings <= 0:
                continue

            yield finding_for(
                bucket,
                rule_id=self.id,
                title=f"Bucket {bucket.resource_id} stores {size_gb:,.0f} GB with no tiering",
                category=self.category,
                action=ACTION_MODIFY_STORAGE,
                savings=savings,
                detail=(
                    f"The bucket holds {size_gb:,.0f} GB entirely in Standard storage with no "
                    "lifecycle transitions and no Intelligent-Tiering configuration. "
                    "Intelligent-Tiering moves objects to cheaper tiers automatically based on "
                    "access patterns, with no retrieval fee for the frequent and infrequent "
                    "tiers."
                ),
                evidence=[
                    Evidence(label="Stored data", value=f"{size_gb:,.1f} GB"),
                    Evidence(
                        label="Objects",
                        value=f"{bucket.metrics.get('object_count', 0):,.0f}",
                    ),
                    Evidence(
                        label="Lifecycle rules",
                        value=str(attributes.get("lifecycle_rule_count", 0)),
                    ),
                    Evidence(label="Intelligent-Tiering", value="not configured"),
                    Evidence(label="Versioning", value=str(attributes.get("versioning"))),
                    Evidence(
                        label="Estimate basis",
                        value=f"assumes {LIFECYCLE_SAVINGS_FRACTION:.0%} of objects are cold",
                    ),
                ],
                remediation=Remediation(
                    summary=(
                        "Enable Intelligent-Tiering for the whole bucket, or add a lifecycle "
                        "rule that transitions objects after 30 to 90 days."
                    ),
                    terraform=(
                        "# aws_s3_bucket_intelligent_tiering_configuration with a single\n"
                        "# filter {} block applies tiering to every object in the bucket."
                    ),
                    console_path=f"S3 > {bucket.resource_id} > Management > Lifecycle rules",
                ),
                confidence="low",
                effort="low",
                risk="low",
                cost_basis="heuristic",
            )


@register
class IncompleteMultipartUploads(Rule):
    """Failed uploads leave parts that bill for storage but are invisible in listings."""

    id = "s3.incomplete_multipart_uploads"
    category = "storage"
    title = "Abandoned multipart uploads"

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        for bucket in ctx.of_type("s3:bucket"):
            attributes = bucket.attributes
            count = attributes.get("incomplete_multipart_uploads")
            if not count:
                continue
            if attributes.get("has_abort_incomplete_upload_rule"):
                continue

            # Parts are not listed with sizes, so no dollar figure is defensible. This is
            # reported for the cleanup rule it justifies, not for a savings number.
            yield finding_for(
                bucket,
                rule_id=self.id,
                title=(
                    f"Bucket {bucket.resource_id} has {count} incomplete multipart upload(s) "
                    "and no cleanup rule"
                ),
                category=self.category,
                action=ACTION_MODIFY_STORAGE,
                savings=0.0,
                detail=(
                    f"{count} multipart upload(s) were started and never completed. Their parts "
                    "bill as storage but do not appear when you list the bucket, so they are "
                    "easy to miss forever. The part sizes are not exposed by the API, so no "
                    "dollar figure is shown here."
                ),
                evidence=[
                    Evidence(label="Incomplete uploads", value=str(count)),
                    Evidence(
                        label="Oldest upload",
                        value=str(attributes.get("oldest_incomplete_upload") or "unknown"),
                    ),
                    Evidence(label="Abort rule configured", value="no"),
                ],
                remediation=Remediation(
                    summary=(
                        "Add a lifecycle rule that aborts incomplete multipart uploads after "
                        "7 days. It is a one-time fix that prevents recurrence."
                    ),
                    terraform=(
                        "# aws_s3_bucket_lifecycle_configuration rule:\n"
                        "#   abort_incomplete_multipart_upload { days_after_initiation = 7 }"
                    ),
                    console_path=f"S3 > {bucket.resource_id} > Management > Lifecycle rules",
                ),
                confidence="high",
                effort="low",
                risk="low",
                cost_basis="heuristic",
            )


@register
class VersioningWithoutExpiry(Rule):
    """Versioned buckets accumulate every overwrite forever unless told otherwise."""

    id = "s3.versioning_without_expiry"
    category = "storage"
    title = "Versioning enabled with no noncurrent version expiry"

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        for bucket in ctx.of_type("s3:bucket"):
            attributes = bucket.attributes
            if attributes.get("versioning") != "Enabled":
                continue
            if attributes.get("has_expiration_rule"):
                continue
            size_gb = (bucket.metrics.get("bucket_size_bytes") or 0) / 1024**3
            if size_gb < MIN_INTERESTING_GB:
                continue

            current_cost = ctx.monthly_cost(bucket)
            # Noncurrent versions are not broken out by the storage metric, so this is
            # explicitly a rule-of-thumb rather than a measurement.
            savings = current_cost * 0.20
            if savings <= 0:
                continue

            yield finding_for(
                bucket,
                rule_id=self.id,
                title=(f"Bucket {bucket.resource_id} keeps every object version indefinitely"),
                category=self.category,
                action=ACTION_MODIFY_STORAGE,
                savings=savings,
                detail=(
                    "Versioning is enabled with no rule to expire noncurrent versions, so every "
                    "overwrite and delete leaves a billable copy behind permanently. The "
                    "CloudWatch storage metric does not separate current from noncurrent data, "
                    "so treat the figure as indicative."
                ),
                evidence=[
                    Evidence(label="Versioning", value="Enabled"),
                    Evidence(label="Noncurrent expiry rule", value="none"),
                    Evidence(label="Stored data", value=f"{size_gb:,.1f} GB"),
                    Evidence(label="Estimate basis", value="assumes 20% is noncurrent versions"),
                ],
                remediation=Remediation(
                    summary=(
                        "Add a lifecycle rule expiring noncurrent versions after 30 to 90 days, "
                        "keeping enough history to recover from a bad deploy."
                    ),
                    terraform=(
                        "# aws_s3_bucket_lifecycle_configuration rule:\n"
                        "#   noncurrent_version_expiration { noncurrent_days = 90 }"
                    ),
                    console_path=f"S3 > {bucket.resource_id} > Management > Lifecycle rules",
                ),
                confidence="low",
                effort="low",
                risk="medium",
                cost_basis="heuristic",
                rollback_possible=False,
            )
