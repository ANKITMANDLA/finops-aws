"""RDS instances, replicas, storage, and snapshots."""

from __future__ import annotations

from collections.abc import Iterable

from finops.instances import rds_graviton_equivalent
from finops.model import (
    ACTION_DELETE,
    ACTION_MIGRATE,
    ACTION_MODIFY_STORAGE,
    ACTION_STOP,
    Evidence,
    Finding,
    Remediation,
)
from finops.rules.base import Rule, RuleContext, finding_for, register
from finops.util import human_money

MIN_INTERESTING_MONTHLY_COST = 5.0


@register
class IdleRdsInstance(Rule):
    """A database nothing connects to."""

    id = "rds.idle_instance"
    category = "database"
    title = "Idle RDS instance"

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        for database in ctx.of_type("rds:db-instance"):
            if database.state != "available":
                continue
            connections_max = database.metrics.get("db_connections_max")
            cpu_avg = database.metrics.get("cpu_avg")
            if connections_max is None:
                continue
            if connections_max > ctx.thresholds.rds_idle_connections:
                continue

            savings = ctx.monthly_cost(database)
            yield finding_for(
                database,
                rule_id=self.id,
                title=f"RDS instance {database.display_name} has no client connections",
                category=self.category,
                action=ACTION_STOP,
                savings=savings,
                detail=(
                    f"Peak connection count over the analysis window was {connections_max:.0f}. "
                    "A database nobody connects to still bills for its instance hours, storage, "
                    "and backups. Note that a stopped RDS instance restarts automatically after "
                    "seven days, so deletion with a final snapshot is the durable fix."
                ),
                evidence=[
                    Evidence(label="Peak connections", value=f"{connections_max:.0f}"),
                    Evidence(
                        label="Average CPU",
                        value=f"{cpu_avg:.1f}%" if cpu_avg is not None else "n/a",
                    ),
                    Evidence(label="Engine", value=str(database.attributes.get("engine"))),
                    Evidence(
                        label="Instance class", value=str(database.attributes.get("instance_class"))
                    ),
                    Evidence(label="Multi-AZ", value=str(database.attributes.get("multi_az"))),
                ],
                remediation=Remediation(
                    summary=(
                        "Take a final snapshot and delete the instance, or stop it if you only "
                        "need a short pause."
                    ),
                    cli=(
                        f"aws rds create-db-snapshot --db-instance-identifier "
                        f"{database.resource_id} --db-snapshot-identifier "
                        f"{database.resource_id}-final --region {database.region}"
                    ),
                    console_path=f"RDS > Databases > {database.resource_id}",
                ),
                confidence="medium",
                effort="low",
                risk="high",
            )


@register
class UnusedReadReplica(Rule):
    """A read replica that serves no reads doubles the instance bill for nothing."""

    id = "rds.unused_read_replica"
    category = "database"
    title = "Unused RDS read replica"

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        for database in ctx.of_type("rds:db-instance"):
            source = database.attributes.get("read_replica_source")
            if not source:
                continue
            connections_max = database.metrics.get("db_connections_max")
            if connections_max is None or connections_max > ctx.thresholds.rds_idle_connections:
                continue

            savings = ctx.monthly_cost(database)
            yield finding_for(
                database,
                rule_id=self.id,
                title=f"Read replica {database.display_name} receives no queries",
                category=self.category,
                action=ACTION_DELETE,
                savings=savings,
                detail=(
                    f"This is a read replica of {source} with a peak of {connections_max:.0f} "
                    "connections, so nothing is reading from it. Unless it exists purely as a "
                    "standby for failover, it is a full-price instance doing no work."
                ),
                evidence=[
                    Evidence(label="Replica of", value=str(source)),
                    Evidence(label="Peak connections", value=f"{connections_max:.0f}"),
                    Evidence(
                        label="Instance class", value=str(database.attributes.get("instance_class"))
                    ),
                ],
                remediation=Remediation(
                    summary=(
                        "Delete the replica. It can be recreated from the source at any time, "
                        "though rebuilding takes as long as the data is large."
                    ),
                    cli=(
                        f"aws rds delete-db-instance --db-instance-identifier "
                        f"{database.resource_id} --skip-final-snapshot "
                        f"--region {database.region}"
                    ),
                    console_path=f"RDS > Databases > {database.resource_id}",
                ),
                confidence="medium",
                effort="low",
                risk="medium",
            )


@register
class RdsGp2Storage(Rule):
    """RDS gp3 storage is cheaper than gp2, same as on EC2."""

    id = "rds.gp2_storage"
    category = "database"
    title = "RDS instance on gp2 storage"

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        for database in ctx.of_type("rds:db-instance"):
            if database.attributes.get("storage_type") != "gp2":
                continue
            storage_gb = database.attributes.get("allocated_storage_gb") or 0
            if not storage_gb:
                continue

            gp2 = ctx.pricing.ebs_gb_month(database.region, "gp2")
            gp3 = ctx.pricing.ebs_gb_month(database.region, "gp3")
            if gp2 is None or gp3 is None:
                continue
            multiplier = 2 if database.attributes.get("multi_az") else 1
            savings = (gp2.amount - gp3.amount) * storage_gb * multiplier
            if savings <= 0:
                continue

            yield finding_for(
                database,
                rule_id=self.id,
                title=f"Move {database.display_name} storage from gp2 to gp3",
                category=self.category,
                action=ACTION_MODIFY_STORAGE,
                savings=savings,
                detail=(
                    f"The instance allocates {storage_gb} GB of gp2 storage"
                    + (" across two availability zones" if multiplier == 2 else "")
                    + ". gp3 costs less per GB and lets you set IOPS independently of size. "
                    "The modification applies online."
                ),
                evidence=[
                    Evidence(label="Current storage type", value="gp2"),
                    Evidence(label="Allocated storage", value=f"{storage_gb} GB"),
                    Evidence(label="Multi-AZ", value=str(bool(multiplier == 2))),
                    Evidence(label="gp2 rate", value=f"${gp2.amount:.3f}/GB-month"),
                    Evidence(label="gp3 rate", value=f"${gp3.amount:.3f}/GB-month"),
                ],
                remediation=Remediation(
                    summary="Modify the instance storage type to gp3 with immediate apply.",
                    cli=(
                        f"aws rds modify-db-instance --db-instance-identifier "
                        f"{database.resource_id} --storage-type gp3 --apply-immediately "
                        f"--region {database.region}"
                    ),
                    terraform='# aws_db_instance: storage_type = "gp3"',
                    console_path=f"RDS > Databases > {database.resource_id} > Modify",
                ),
                confidence="high",
                effort="low",
                risk="low",
                cost_basis="list_price_estimate",
            )


@register
class RdsGravitonCandidate(Rule):
    """Graviton database instance classes cost less for the same size."""

    id = "rds.graviton_candidate"
    category = "database"
    title = "RDS Graviton migration candidate"

    GRAVITON_DISCOUNT = 0.10

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        for database in ctx.of_type("rds:db-instance"):
            instance_class = database.attributes.get("instance_class") or ""
            replacement = rds_graviton_equivalent(instance_class)
            if not replacement:
                continue
            current_cost = ctx.monthly_cost(database)
            if current_cost < MIN_INTERESTING_MONTHLY_COST:
                continue

            engine = database.attributes.get("engine") or ""
            new_cost = ctx.pricing.rds_instance_monthly(
                database.region,
                replacement,
                engine,
                multi_az=bool(database.attributes.get("multi_az")),
            )
            savings = (
                current_cost - new_cost
                if new_cost is not None
                else current_cost * self.GRAVITON_DISCOUNT
            )
            if savings <= 0:
                continue

            yield finding_for(
                database,
                rule_id=self.id,
                title=f"Move {database.display_name} to {replacement}",
                category=self.category,
                action=ACTION_MIGRATE,
                savings=savings,
                detail=(
                    "Graviton database instance classes cost less per hour than their x86 "
                    "equivalents. Unlike application servers, the database engine is supplied by "
                    "AWS, so the switch needs no code changes; it is a modify-and-restart."
                ),
                evidence=[
                    Evidence(label="Current class", value=instance_class),
                    Evidence(label="Recommended class", value=replacement),
                    Evidence(label="Engine", value=engine),
                    Evidence(label="Current cost", value=f"{human_money(current_cost)}/month"),
                ],
                remediation=Remediation(
                    summary=(
                        "Modify the instance class during a maintenance window. Confirm your "
                        "engine version supports the Graviton class first."
                    ),
                    cli=(
                        f"aws rds modify-db-instance --db-instance-identifier "
                        f"{database.resource_id} --db-instance-class {replacement} "
                        f"--region {database.region}"
                    ),
                    console_path=f"RDS > Databases > {database.resource_id} > Modify",
                ),
                confidence="medium",
                effort="medium",
                risk="medium",
            )


@register
class StaleRdsSnapshot(Rule):
    """Manual RDS snapshots never expire on their own."""

    id = "rds.stale_manual_snapshot"
    category = "database"
    title = "Stale manual RDS snapshot"

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        for snapshot in ctx.of_type("rds:snapshot"):
            age = ctx.age_days(snapshot)
            if age is None or age < ctx.thresholds.snapshot_stale_age_days:
                continue
            savings = ctx.monthly_cost(snapshot)
            if savings <= 0:
                continue

            source = snapshot.attributes.get("db_instance_identifier")
            yield finding_for(
                snapshot,
                rule_id=self.id,
                title=f"Manual RDS snapshot {snapshot.resource_id} is {age:.0f} days old",
                category=self.category,
                action=ACTION_DELETE,
                savings=savings,
                detail=(
                    "Manual snapshots are kept until someone deletes them, unlike automated "
                    "backups which expire with the retention window. This one has been sitting "
                    f"for {age:.0f} days."
                ),
                evidence=[
                    Evidence(label="Age", value=f"{age:.0f} days"),
                    Evidence(label="Source database", value=str(source or "unknown")),
                    Evidence(
                        label="Size",
                        value=f"{snapshot.attributes.get('allocated_storage_gb')} GB",
                    ),
                    Evidence(label="Engine", value=str(snapshot.attributes.get("engine"))),
                ],
                remediation=Remediation(
                    summary="Delete the snapshot if the retention requirement has passed.",
                    cli=(
                        f"aws rds delete-db-snapshot --db-snapshot-identifier "
                        f"{snapshot.resource_id} --region {snapshot.region}"
                    ),
                    console_path="RDS > Snapshots",
                ),
                confidence="medium",
                effort="low",
                risk="medium",
                rollback_possible=False,
            )
