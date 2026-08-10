"""EKS clusters, managed node groups, and Fargate profiles.

Every cluster carries a fixed control-plane charge, so an under-used cluster is one of
the more expensive things to leave running. Node group capacity type (On-Demand vs Spot)
and scaling configuration are captured because they drive most EKS compute spend.
"""

from __future__ import annotations

import logging

from botocore.exceptions import BotoCoreError, ClientError

from finops.aws.collectors.base import CollectionContext, Collector, paginate, register
from finops.model import Resource

logger = logging.getLogger(__name__)

# Published EKS control plane price: $0.10 per cluster-hour in commercial regions.
EKS_CONTROL_PLANE_HOURLY_USD = 0.10


@register
class EksCollector(Collector):
    key = "eks"
    service = "EKS"

    def collect(self, ctx: CollectionContext, region: str) -> list[Resource]:
        client = ctx.client("eks", region)
        cluster_names = list(paginate(client, "list_clusters", "clusters"))
        resources: list[Resource] = []

        for name in cluster_names:
            try:
                cluster = client.describe_cluster(name=name)["cluster"]
            except (ClientError, BotoCoreError) as exc:
                logger.debug("describe_cluster failed for %s: %s", name, exc)
                continue

            nodegroups = self._collect_nodegroups(ctx, client, region, name, cluster["arn"])
            fargate_profiles = self._collect_fargate_profiles(ctx, client, region, name)

            logging_types = [
                entry
                for setup in cluster.get("logging", {}).get("clusterLogging", [])
                if setup.get("enabled")
                for entry in setup.get("types", [])
            ]
            resources.append(
                Resource(
                    arn=cluster["arn"],
                    resource_id=name,
                    resource_type="eks:cluster",
                    service="EKS",
                    region=region,
                    account_id=ctx.account_id,
                    name=name,
                    state=cluster.get("status"),
                    created_at=cluster.get("createdAt"),
                    tags=dict(cluster.get("tags") or {}),
                    attributes={
                        "version": cluster.get("version"),
                        "platform_version": cluster.get("platformVersion"),
                        "endpoint_public_access": cluster.get("resourcesVpcConfig", {}).get(
                            "endpointPublicAccess"
                        ),
                        "endpoint_private_access": cluster.get("resourcesVpcConfig", {}).get(
                            "endpointPrivateAccess"
                        ),
                        "enabled_logging_types": logging_types,
                        "nodegroup_count": len(nodegroups),
                        "fargate_profile_count": len(fargate_profiles),
                        "total_node_desired": sum(
                            n.attributes.get("desired_size") or 0 for n in nodegroups
                        ),
                        "support_type": cluster.get("upgradePolicy", {}).get("supportType"),
                        "control_plane_monthly_usd": round(EKS_CONTROL_PLANE_HOURLY_USD * 730, 2),
                    },
                )
            )
            resources.extend(nodegroups)
            resources.extend(fargate_profiles)
        return resources

    def _collect_nodegroups(
        self, ctx: CollectionContext, client, region: str, cluster_name: str, cluster_arn: str
    ) -> list[Resource]:
        resources: list[Resource] = []
        try:
            names = list(
                paginate(client, "list_nodegroups", "nodegroups", clusterName=cluster_name)
            )
        except (ClientError, BotoCoreError) as exc:
            logger.debug("list_nodegroups failed for %s: %s", cluster_name, exc)
            return resources

        for nodegroup_name in names:
            try:
                group = client.describe_nodegroup(
                    clusterName=cluster_name, nodegroupName=nodegroup_name
                )["nodegroup"]
            except (ClientError, BotoCoreError) as exc:
                logger.debug("describe_nodegroup failed for %s: %s", nodegroup_name, exc)
                continue
            scaling = group.get("scalingConfig", {})
            resources.append(
                Resource(
                    arn=group["nodegroupArn"],
                    resource_id=f"{cluster_name}/{nodegroup_name}",
                    resource_type="eks:nodegroup",
                    service="EKS",
                    region=region,
                    account_id=ctx.account_id,
                    name=nodegroup_name,
                    state=group.get("status"),
                    created_at=group.get("createdAt"),
                    tags=dict(group.get("tags") or {}),
                    attributes={
                        "cluster_name": cluster_name,
                        "cluster_arn": cluster_arn,
                        "instance_types": group.get("instanceTypes", []),
                        "capacity_type": group.get("capacityType", "ON_DEMAND"),
                        "ami_type": group.get("amiType"),
                        "disk_size_gb": group.get("diskSize"),
                        "min_size": scaling.get("minSize"),
                        "max_size": scaling.get("maxSize"),
                        "desired_size": scaling.get("desiredSize"),
                        "subnets": group.get("subnets", []),
                        "labels": group.get("labels", {}),
                        "taints": group.get("taints", []),
                        "auto_scaling_groups": [
                            g.get("name")
                            for g in group.get("resources", {}).get("autoScalingGroups", [])
                        ],
                    },
                )
            )
        return resources

    def _collect_fargate_profiles(
        self, ctx: CollectionContext, client, region: str, cluster_name: str
    ) -> list[Resource]:
        resources: list[Resource] = []
        try:
            names = list(
                paginate(
                    client, "list_fargate_profiles", "fargateProfileNames", clusterName=cluster_name
                )
            )
        except (ClientError, BotoCoreError) as exc:
            logger.debug("list_fargate_profiles failed for %s: %s", cluster_name, exc)
            return resources

        for profile_name in names:
            try:
                profile = client.describe_fargate_profile(
                    clusterName=cluster_name, fargateProfileName=profile_name
                )["fargateProfile"]
            except (ClientError, BotoCoreError) as exc:
                logger.debug("describe_fargate_profile failed for %s: %s", profile_name, exc)
                continue
            resources.append(
                Resource(
                    arn=profile["fargateProfileArn"],
                    resource_id=f"{cluster_name}/{profile_name}",
                    resource_type="eks:fargate-profile",
                    service="EKS",
                    region=region,
                    account_id=ctx.account_id,
                    name=profile_name,
                    state=profile.get("status"),
                    created_at=profile.get("createdAt"),
                    tags=dict(profile.get("tags") or {}),
                    attributes={
                        "cluster_name": cluster_name,
                        "selectors": profile.get("selectors", []),
                        "subnets": profile.get("subnets", []),
                    },
                )
            )
        return resources
