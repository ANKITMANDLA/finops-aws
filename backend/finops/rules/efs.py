"""EFS file systems that are over-provisioned, never tiered, or mounted by nobody.

EFS waste is quieter than EC2 waste. Nothing shows up as an idle instance: a file system
with 100 MiB/s provisioned and 5 MiB/s of traffic looks identical in the console to one
that is sized correctly, and cold data sitting in Standard looks identical to cold data
that has been tiered. Both are charged for every hour.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

from finops.aws.pricing import EFS_BASELINE_MIBPS_PER_GB, efs_billable_throughput_mibps
from finops.model import (
    ACTION_DELETE,
    ACTION_MODIFY_STORAGE,
    ACTION_RIGHTSIZE,
    Evidence,
    Finding,
    Remediation,
    Resource,
)
from finops.rules.base import Rule, RuleContext, finding_for, register
from finops.util import human_money

# Leave half again as much throughput as the busiest hour needed. Provisioned throughput
# exists to absorb peaks, so a recommendation that trims to the observed maximum exactly
# would be a recommendation to start throttling.
THROUGHPUT_HEADROOM = 1.5

# EFS Standard is one of the more expensive per-GB stores AWS sells, so tiering is worth
# looking at on much smaller file systems than S3 would be. Below this, the minimum
# savings filter drops the finding anyway.
MIN_INTERESTING_GB = 20

# Share of Standard data assumed to be cold enough for Infrequent Access. AWS reports most
# file systems run far higher than this; the conservative figure keeps the estimate from
# depending on a number nobody here has measured.
COLD_FRACTION = 0.50

# Move files to Infrequent Access once they have gone a month untouched, and back to
# Standard the moment anything reads them again.
_LIFECYCLE_POLICIES = (
    '[{"TransitionToIA":"AFTER_30_DAYS"},{"TransitionToPrimaryStorageClass":"AFTER_1_ACCESS"}]'
)


def _label(file_system: Resource) -> str:
    """Name and id together, because one Name tag is often shared by every file system."""
    name = file_system.display_name
    if name == file_system.resource_id:
        return name
    return f"{name} ({file_system.resource_id})"


@register
class OverProvisionedEfsThroughput(Rule):
    """Provisioned throughput far above the busiest hour the file system has seen."""

    id = "efs.overprovisioned_throughput"
    category = "storage"
    title = "Over-provisioned EFS throughput"

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        for file_system in ctx.of_type("efs:file-system"):
            attributes = file_system.attributes
            provisioned = attributes.get("provisioned_throughput_mibps") or 0.0
            if attributes.get("throughput_mode") != "provisioned" or not provisioned:
                continue

            standard_gb = float(attributes.get("standard_gb") or 0.0)
            billable = efs_billable_throughput_mibps(standard_gb, "provisioned", provisioned)
            # Provisioned below its own baseline is free, and EFS silently serves it from
            # Bursting instead, so there is nothing to save.
            if billable <= 0:
                continue

            peak = file_system.metrics.get("efs_throughput_mibps_peak")
            average = file_system.metrics.get("efs_throughput_mibps_avg")
            if peak is None:
                continue
            if peak > provisioned * ctx.thresholds.efs_throughput_overprovisioned_ratio:
                continue

            target = max(math.ceil(peak * THROUGHPUT_HEADROOM), 1)
            if target >= provisioned:
                continue
            price = ctx.pricing.efs_provisioned_throughput_month(file_system.region)
            if price is None:
                continue
            savings = (
                billable - efs_billable_throughput_mibps(standard_gb, "provisioned", target)
            ) * price.amount
            if savings <= 0:
                continue
            included = standard_gb * EFS_BASELINE_MIBPS_PER_GB

            yield finding_for(
                file_system,
                rule_id=self.id,
                title=(
                    f"EFS {_label(file_system)} provisions {provisioned:,.0f} MiB/s but "
                    f"peaks at {peak:,.1f}"
                ),
                category=self.category,
                action=ACTION_RIGHTSIZE,
                savings=savings,
                detail=(
                    f"Throughput is provisioned at {provisioned:,.0f} MiB/s while the busiest "
                    f"hour measured reached {peak:,.1f} MiB/s. Provisioning {target:,} MiB/s "
                    "still leaves half again as much headroom as that peak needed. The charge "
                    f"only applies above the {included:,.2f} MiB/s this file system's Standard "
                    "storage already includes, which is why the saving is smaller than the raw "
                    "reduction. Elastic throughput is the other option, but it bills per GB "
                    "read and written rather than per MiB/s, so it only wins on workloads that "
                    "sit idle most of the time."
                ),
                evidence=[
                    Evidence(label="Provisioned throughput", value=f"{provisioned:,.0f} MiB/s"),
                    Evidence(label="Peak hour observed", value=f"{peak:,.1f} MiB/s"),
                    Evidence(
                        label="Average observed",
                        value=f"{average:,.1f} MiB/s" if average is not None else "unknown",
                    ),
                    Evidence(label="Suggested throughput", value=f"{target:,} MiB/s"),
                    Evidence(
                        label="Included with stored data",
                        value=f"{included:,.2f} MiB/s from {standard_gb:,.1f} GB in Standard",
                    ),
                    Evidence(label="Billable today", value=f"{billable:,.2f} MiB/s"),
                    Evidence(label="Rate", value=f"${price.amount:,.2f}/MiBps-month"),
                ],
                remediation=Remediation(
                    summary=(
                        "Lower the provisioned throughput. It applies immediately and needs no "
                        "downtime, but AWS allows only one decrease per 24 hours, so step down "
                        "in one move rather than several."
                    ),
                    cli=(
                        f"aws efs update-file-system --file-system-id {file_system.resource_id} "
                        f"--throughput-mode provisioned --provisioned-throughput-in-mibps "
                        f"{target} --region {file_system.region}"
                    ),
                    terraform=(
                        f"# aws_efs_file_system: provisioned_throughput_in_mibps = {target}"
                    ),
                    console_path="EFS > File systems > Edit",
                ),
                confidence="medium",
                effort="low",
                risk="medium",
                cost_basis="list_price_estimate",
            )


@register
class EfsWithoutLifecyclePolicy(Rule):
    """Cold data staying in Standard at ten times the Infrequent Access rate."""

    id = "efs.no_lifecycle_policy"
    category = "storage"
    title = "EFS file system without lifecycle management"

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        for file_system in ctx.of_type("efs:file-system"):
            attributes = file_system.attributes
            if attributes.get("transition_to_ia"):
                continue
            standard_gb = float(attributes.get("standard_gb") or 0.0)
            if standard_gb < MIN_INTERESTING_GB:
                continue

            one_zone = bool(attributes.get("one_zone"))
            elastic = attributes.get("throughput_mode") == "elastic"
            standard_price = ctx.pricing.efs_storage_gb_month(
                file_system.region, "standard", one_zone=one_zone, elastic=elastic
            )
            ia_price = ctx.pricing.efs_storage_gb_month(
                file_system.region, "ia", one_zone=one_zone, elastic=elastic
            )
            if standard_price is None or ia_price is None:
                continue
            savings = standard_gb * COLD_FRACTION * (standard_price.amount - ia_price.amount)
            if savings <= 0:
                continue

            yield finding_for(
                file_system,
                rule_id=self.id,
                title=(
                    f"EFS {_label(file_system)} holds {standard_gb:,.0f} GB in Standard "
                    "with no tiering"
                ),
                category=self.category,
                action=ACTION_MODIFY_STORAGE,
                savings=savings,
                detail=(
                    f"Every byte written to this file system stays in Standard at "
                    f"${standard_price.amount:.3f}/GB-month, because no lifecycle policy moves "
                    f"it anywhere. Infrequent Access costs ${ia_price.amount:.3f}/GB-month, and "
                    "a lifecycle policy moves files there automatically once they have gone "
                    "untouched for a set number of days. Reads from Infrequent Access are "
                    "charged per GB, so the saving assumes the tiered data really is cold."
                ),
                evidence=[
                    Evidence(label="Standard storage", value=f"{standard_gb:,.1f} GB"),
                    Evidence(
                        label="Infrequent Access storage",
                        value=f"{float(attributes.get('ia_gb') or 0.0):,.1f} GB",
                    ),
                    Evidence(label="Lifecycle policy", value="none"),
                    Evidence(label="Standard rate", value=f"${standard_price.amount:.3f}/GB-month"),
                    Evidence(
                        label="Infrequent Access rate", value=f"${ia_price.amount:.3f}/GB-month"
                    ),
                    Evidence(
                        label="Estimate basis",
                        value=f"assumes {COLD_FRACTION:.0%} of Standard data is untouched for "
                        "30 days",
                    ),
                ],
                remediation=Remediation(
                    summary=(
                        "Set a lifecycle policy transitioning to Infrequent Access after 30 "
                        "days. Files move back to Standard on first read, so nothing breaks if "
                        "something turns out to be warm after all."
                    ),
                    cli=(
                        "aws efs put-lifecycle-configuration --file-system-id "
                        f"{file_system.resource_id} "
                        f"--lifecycle-policies '{_LIFECYCLE_POLICIES}' "
                        f"--region {file_system.region}"
                    ),
                    terraform=(
                        "# aws_efs_file_system lifecycle_policy blocks:\n"
                        '#   transition_to_ia = "AFTER_30_DAYS"\n'
                        '#   transition_to_primary_storage_class = "AFTER_1_ACCESS"'
                    ),
                    console_path="EFS > File systems > Edit > Lifecycle management",
                ),
                confidence="medium",
                effort="low",
                risk="low",
                cost_basis="heuristic",
            )


@register
class UnusedEfsFileSystem(Rule):
    """A file system nothing has connected to, still billing for what it holds."""

    id = "efs.unused_file_system"
    category = "idle"
    title = "Unused EFS file system"

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        for file_system in ctx.of_type("efs:file-system"):
            if file_system.state != "available":
                continue
            attributes = file_system.attributes
            # The destination of a replication pair is written to by AWS, not by clients,
            # so it has no connections of its own and must not be deleted.
            if attributes.get("replication_overwrite_protection") == "DISABLED":
                continue
            age = ctx.age_days(file_system)
            if age is not None and age < ctx.thresholds.efs_unused_min_age_days:
                continue

            mount_targets = attributes.get("mount_target_count") or 0
            connections = file_system.metrics.get("efs_client_connections_max")
            if mount_targets:
                # With a mount target in place, only CloudWatch can say whether anything
                # actually used it.
                if connections is None:
                    continue
                if connections > ctx.thresholds.efs_idle_connections:
                    continue

            savings = ctx.monthly_cost(file_system)
            if savings <= 0:
                continue

            reason = (
                "has no mount target, so nothing in the VPC can reach it"
                if not mount_targets
                else "has had no client connections at all"
            )
            yield finding_for(
                file_system,
                rule_id=self.id,
                title=(
                    f"EFS {_label(file_system)} holds "
                    f"{float(attributes.get('size_gb') or 0.0):,.1f} GB and is unused"
                ),
                category=self.category,
                action=ACTION_DELETE,
                savings=savings,
                detail=(
                    f"This file system {reason}, and it is {age:,.0f} days old. It bills for "
                    "stored data, and for provisioned throughput if any is configured, "
                    "regardless of whether anything mounts it."
                    if age is not None
                    else f"This file system {reason}. It bills for stored data regardless of "
                    "whether anything mounts it."
                ),
                evidence=[
                    Evidence(label="Mount targets", value=str(mount_targets)),
                    Evidence(
                        label="Peak client connections",
                        value=f"{connections:,.0f}" if connections is not None else "none seen",
                    ),
                    Evidence(
                        label="Stored data",
                        value=f"{float(attributes.get('size_gb') or 0.0):,.1f} GB",
                    ),
                    Evidence(label="Age", value=f"{age:,.0f} days" if age else "unknown"),
                    Evidence(label="Current cost", value=f"{human_money(savings)}/month"),
                ],
                remediation=Remediation(
                    summary=(
                        "Confirm nothing intends to mount it, take an AWS Backup recovery point "
                        "if the contents may matter, then delete the file system. Deletion "
                        "destroys the data."
                    ),
                    cli=(
                        f"aws efs describe-mount-targets --file-system-id "
                        f"{file_system.resource_id} --region {file_system.region}\n"
                        f"aws efs delete-file-system --file-system-id "
                        f"{file_system.resource_id} --region {file_system.region}"
                    ),
                    console_path="EFS > File systems",
                ),
                confidence="medium",
                effort="low",
                risk="high",
                rollback_possible=False,
            )
