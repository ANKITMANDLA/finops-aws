"""EC2 instances and Auto Scaling groups."""

from __future__ import annotations

from typing import Any

from finops.aws.collectors.base import (
    CollectionContext,
    Collector,
    paginate,
    register,
    synthesize_arn,
    tags_to_dict,
)
from finops.model import Resource


def _instance_lifecycle(instance: dict[str, Any]) -> str:
    """on-demand, spot, or capacity-block. Spot instances are already discounted, so
    rules must not offer Spot as a saving for them."""
    lifecycle = instance.get("InstanceLifecycle")
    if lifecycle == "spot":
        return "spot"
    if lifecycle == "scheduled":
        return "scheduled"
    if lifecycle == "capacity-block":
        return "capacity-block"
    return "on-demand"


@register
class Ec2InstanceCollector(Collector):
    key = "ec2"
    service = "EC2"

    def collect(self, ctx: CollectionContext, region: str) -> list[Resource]:
        client = ctx.client("ec2", region)
        resources: list[Resource] = []
        for reservation in paginate(client, "describe_instances", "Reservations"):
            for instance in reservation.get("Instances", []):
                resources.append(self._build(ctx, region, instance))
        return resources

    def _build(self, ctx: CollectionContext, region: str, instance: dict[str, Any]) -> Resource:
        tags = tags_to_dict(instance.get("Tags"))
        instance_id = instance["InstanceId"]
        cpu_options = instance.get("CpuOptions", {})
        vcpus = cpu_options.get("CoreCount", 0) * cpu_options.get("ThreadsPerCore", 1) or None
        volume_ids = [
            mapping["Ebs"]["VolumeId"]
            for mapping in instance.get("BlockDeviceMappings", [])
            if mapping.get("Ebs", {}).get("VolumeId")
        ]
        return Resource(
            arn=synthesize_arn("ec2", region, ctx.account_id, f"instance/{instance_id}"),
            resource_id=instance_id,
            resource_type="ec2:instance",
            service="EC2",
            region=region,
            account_id=ctx.account_id,
            name=tags.get("Name"),
            availability_zone=instance.get("Placement", {}).get("AvailabilityZone"),
            state=instance.get("State", {}).get("Name"),
            created_at=instance.get("LaunchTime"),
            tags=tags,
            attributes={
                "instance_type": instance.get("InstanceType"),
                "lifecycle": _instance_lifecycle(instance),
                "tenancy": instance.get("Placement", {}).get("Tenancy", "default"),
                "platform": instance.get("Platform") or "linux",
                "platform_details": instance.get("PlatformDetails", "Linux/UNIX"),
                # Needed to price the exact billing flavor (RunInstances:0002 etc).
                "usage_operation": instance.get("UsageOperation"),
                "architecture": instance.get("Architecture"),
                "vcpus": vcpus,
                "image_id": instance.get("ImageId"),
                "vpc_id": instance.get("VpcId"),
                "subnet_id": instance.get("SubnetId"),
                "key_name": instance.get("KeyName"),
                "ebs_optimized": instance.get("EbsOptimized", False),
                "detailed_monitoring": instance.get("Monitoring", {}).get("State") == "enabled",
                "public_ip": instance.get("PublicIpAddress"),
                "attached_volume_ids": volume_ids,
                "state_transition_reason": instance.get("StateTransitionReason") or None,
                "auto_scaling_group": tags.get("aws:autoscaling:groupName"),
            },
        )


@register
class AutoScalingGroupCollector(Collector):
    key = "autoscaling"
    service = "AutoScaling"

    def collect(self, ctx: CollectionContext, region: str) -> list[Resource]:
        client = ctx.client("autoscaling", region)
        resources: list[Resource] = []
        for group in paginate(client, "describe_auto_scaling_groups", "AutoScalingGroups"):
            tags = tags_to_dict(group.get("Tags"))
            instance_types = sorted(
                {i.get("InstanceType") for i in group.get("Instances", []) if i.get("InstanceType")}
            )
            mixed_policy = group.get("MixedInstancesPolicy") or {}
            spot_options = mixed_policy.get("InstancesDistribution", {}) if mixed_policy else {}
            resources.append(
                Resource(
                    arn=group["AutoScalingGroupARN"],
                    resource_id=group["AutoScalingGroupName"],
                    resource_type="autoscaling:group",
                    service="AutoScaling",
                    region=region,
                    account_id=ctx.account_id,
                    name=group["AutoScalingGroupName"],
                    state=group.get("Status") or "active",
                    created_at=group.get("CreatedTime"),
                    tags=tags,
                    attributes={
                        "min_size": group.get("MinSize"),
                        "max_size": group.get("MaxSize"),
                        "desired_capacity": group.get("DesiredCapacity"),
                        "instance_count": len(group.get("Instances", [])),
                        "instance_types": instance_types,
                        "availability_zones": group.get("AvailabilityZones", []),
                        "uses_mixed_instances": bool(mixed_policy),
                        "spot_allocation_strategy": spot_options.get("SpotAllocationStrategy"),
                        "on_demand_base_capacity": spot_options.get("OnDemandBaseCapacity"),
                        "on_demand_percentage": spot_options.get(
                            "OnDemandPercentageAboveBaseCapacity"
                        ),
                        "suspended_processes": [
                            p.get("ProcessName") for p in group.get("SuspendedProcesses", [])
                        ],
                        "health_check_type": group.get("HealthCheckType"),
                        "target_group_arns": group.get("TargetGroupARNs", []),
                        "capacity_rebalance": group.get("CapacityRebalance"),
                    },
                )
            )
        return resources
