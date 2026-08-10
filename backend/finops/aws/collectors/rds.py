"""RDS instances, Aurora clusters, and manual snapshots."""

from __future__ import annotations

from finops.aws.collectors.base import (
    CollectionContext,
    Collector,
    paginate,
    register,
    tags_to_dict,
)
from finops.model import Resource


@register
class RdsInstanceCollector(Collector):
    key = "rds"
    service = "RDS"

    def collect(self, ctx: CollectionContext, region: str) -> list[Resource]:
        client = ctx.client("rds", region)
        resources: list[Resource] = []
        for instance in paginate(client, "describe_db_instances", "DBInstances"):
            identifier = instance["DBInstanceIdentifier"]
            tags = tags_to_dict(instance.get("TagList"))
            resources.append(
                Resource(
                    arn=instance["DBInstanceArn"],
                    resource_id=identifier,
                    resource_type="rds:db-instance",
                    service="RDS",
                    region=region,
                    account_id=ctx.account_id,
                    name=tags.get("Name") or identifier,
                    availability_zone=instance.get("AvailabilityZone"),
                    state=instance.get("DBInstanceStatus"),
                    created_at=instance.get("InstanceCreateTime"),
                    tags=tags,
                    attributes={
                        "instance_class": instance.get("DBInstanceClass"),
                        "engine": instance.get("Engine"),
                        "engine_version": instance.get("EngineVersion"),
                        "multi_az": instance.get("MultiAZ", False),
                        "allocated_storage_gb": instance.get("AllocatedStorage"),
                        "max_allocated_storage_gb": instance.get("MaxAllocatedStorage"),
                        "storage_type": instance.get("StorageType"),
                        "iops": instance.get("Iops"),
                        "storage_throughput": instance.get("StorageThroughput"),
                        "storage_encrypted": instance.get("StorageEncrypted", False),
                        "backup_retention_days": instance.get("BackupRetentionPeriod"),
                        "performance_insights": instance.get("PerformanceInsightsEnabled", False),
                        "performance_insights_retention_days": instance.get(
                            "PerformanceInsightsRetentionPeriod"
                        ),
                        # Present only on replicas; a replica with no traffic is a common waste.
                        "read_replica_source": instance.get(
                            "ReadReplicaSourceDBInstanceIdentifier"
                        ),
                        "read_replica_ids": instance.get("ReadReplicaDBInstanceIdentifiers", []),
                        "cluster_identifier": instance.get("DBClusterIdentifier"),
                        "publicly_accessible": instance.get("PubliclyAccessible", False),
                        "deletion_protection": instance.get("DeletionProtection", False),
                        "license_model": instance.get("LicenseModel"),
                        "auto_minor_version_upgrade": instance.get("AutoMinorVersionUpgrade"),
                    },
                )
            )
        return resources


@register
class RdsClusterCollector(Collector):
    key = "rds-cluster"
    service = "RDS"

    def collect(self, ctx: CollectionContext, region: str) -> list[Resource]:
        client = ctx.client("rds", region)
        resources: list[Resource] = []
        for cluster in paginate(client, "describe_db_clusters", "DBClusters"):
            identifier = cluster["DBClusterIdentifier"]
            tags = tags_to_dict(cluster.get("TagList"))
            serverless = cluster.get("ServerlessV2ScalingConfiguration") or {}
            resources.append(
                Resource(
                    arn=cluster["DBClusterArn"],
                    resource_id=identifier,
                    resource_type="rds:db-cluster",
                    service="RDS",
                    region=region,
                    account_id=ctx.account_id,
                    name=tags.get("Name") or identifier,
                    state=cluster.get("Status"),
                    created_at=cluster.get("ClusterCreateTime"),
                    tags=tags,
                    attributes={
                        "engine": cluster.get("Engine"),
                        "engine_mode": cluster.get("EngineMode"),
                        "engine_version": cluster.get("EngineVersion"),
                        "member_count": len(cluster.get("DBClusterMembers", [])),
                        "members": [
                            m.get("DBInstanceIdentifier")
                            for m in cluster.get("DBClusterMembers", [])
                        ],
                        "multi_az": cluster.get("MultiAZ", False),
                        "backup_retention_days": cluster.get("BackupRetentionPeriod"),
                        "storage_type": cluster.get("StorageType"),
                        "allocated_storage_gb": cluster.get("AllocatedStorage"),
                        "serverless_min_acu": serverless.get("MinCapacity"),
                        "serverless_max_acu": serverless.get("MaxCapacity"),
                        "availability_zones": cluster.get("AvailabilityZones", []),
                        "deletion_protection": cluster.get("DeletionProtection", False),
                    },
                )
            )
        return resources


@register
class RdsSnapshotCollector(Collector):
    key = "rds-snapshot"
    service = "RDS Snapshots"

    def collect(self, ctx: CollectionContext, region: str) -> list[Resource]:
        client = ctx.client("rds", region)
        resources: list[Resource] = []
        # Manual snapshots persist until deleted; automated ones expire on their own.
        for snapshot in paginate(
            client, "describe_db_snapshots", "DBSnapshots", SnapshotType="manual"
        ):
            identifier = snapshot["DBSnapshotIdentifier"]
            tags = tags_to_dict(snapshot.get("TagList"))
            resources.append(
                Resource(
                    arn=snapshot["DBSnapshotArn"],
                    resource_id=identifier,
                    resource_type="rds:snapshot",
                    service="RDS Snapshots",
                    region=region,
                    account_id=ctx.account_id,
                    name=tags.get("Name") or identifier,
                    availability_zone=snapshot.get("AvailabilityZone"),
                    state=snapshot.get("Status"),
                    created_at=snapshot.get("SnapshotCreateTime"),
                    tags=tags,
                    attributes={
                        "db_instance_identifier": snapshot.get("DBInstanceIdentifier"),
                        "allocated_storage_gb": snapshot.get("AllocatedStorage"),
                        "engine": snapshot.get("Engine"),
                        "encrypted": snapshot.get("Encrypted", False),
                        "snapshot_type": snapshot.get("SnapshotType"),
                    },
                )
            )
        return resources
