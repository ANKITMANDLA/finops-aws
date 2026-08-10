"""Application, Network, and Classic load balancers, including target health."""

from __future__ import annotations

import logging

from botocore.exceptions import BotoCoreError, ClientError

from finops.aws.collectors.base import (
    CollectionContext,
    Collector,
    paginate,
    register,
    synthesize_arn,
    tags_to_dict,
)
from finops.model import Resource
from finops.util import chunked

logger = logging.getLogger(__name__)

# describe_tags accepts at most 20 ARNs per call.
_TAG_BATCH_SIZE = 20


@register
class LoadBalancerV2Collector(Collector):
    """ALB, NLB, and Gateway Load Balancers.

    Target health is the deciding signal for "this load balancer costs money but serves
    nothing", so it is collected here rather than left to a rule.
    """

    key = "elbv2"
    service = "ELB"

    def collect(self, ctx: CollectionContext, region: str) -> list[Resource]:
        client = ctx.client("elbv2", region)
        balancers = list(paginate(client, "describe_load_balancers", "LoadBalancers"))
        if not balancers:
            return []

        tags_by_arn = self._fetch_tags(client, [lb["LoadBalancerArn"] for lb in balancers])
        target_groups_by_lb = self._fetch_target_groups(client, balancers)

        resources: list[Resource] = []
        for balancer in balancers:
            arn = balancer["LoadBalancerArn"]
            tags = tags_by_arn.get(arn, {})
            groups = target_groups_by_lb.get(arn, [])
            total_targets = sum(g["target_count"] for g in groups)
            healthy_targets = sum(g["healthy_count"] for g in groups)
            resources.append(
                Resource(
                    arn=arn,
                    resource_id=balancer["LoadBalancerName"],
                    resource_type=f"elbv2:{balancer.get('Type', 'application')}",
                    service="ELB",
                    region=region,
                    account_id=ctx.account_id,
                    name=balancer["LoadBalancerName"],
                    state=balancer.get("State", {}).get("Code"),
                    created_at=balancer.get("CreatedTime"),
                    tags=tags,
                    attributes={
                        "lb_type": balancer.get("Type", "application"),
                        "scheme": balancer.get("Scheme"),
                        "dns_name": balancer.get("DNSName"),
                        "vpc_id": balancer.get("VpcId"),
                        "availability_zones": [
                            az.get("ZoneName") for az in balancer.get("AvailabilityZones", [])
                        ],
                        "ip_address_type": balancer.get("IpAddressType"),
                        "target_group_count": len(groups),
                        "target_count": total_targets,
                        "healthy_target_count": healthy_targets,
                        "target_groups": groups,
                    },
                )
            )
        return resources

    def _fetch_tags(self, client, arns: list[str]) -> dict[str, dict[str, str]]:
        tags: dict[str, dict[str, str]] = {}
        for batch in chunked(arns, _TAG_BATCH_SIZE):
            try:
                response = client.describe_tags(ResourceArns=batch)
            except (ClientError, BotoCoreError) as exc:
                logger.debug("describe_tags failed for load balancers: %s", exc)
                continue
            for description in response.get("TagDescriptions", []):
                tags[description["ResourceArn"]] = tags_to_dict(description.get("Tags"))
        return tags

    def _fetch_target_groups(self, client, balancers: list[dict]) -> dict[str, list[dict]]:
        """Map load balancer ARN to its target groups with registered/healthy counts."""
        result: dict[str, list[dict]] = {}
        for balancer in balancers:
            lb_arn = balancer["LoadBalancerArn"]
            try:
                groups = list(
                    paginate(
                        client,
                        "describe_target_groups",
                        "TargetGroups",
                        LoadBalancerArn=lb_arn,
                    )
                )
            except (ClientError, BotoCoreError) as exc:
                logger.debug("describe_target_groups failed for %s: %s", lb_arn, exc)
                continue

            summaries: list[dict] = []
            for group in groups:
                target_count = 0
                healthy_count = 0
                try:
                    health = client.describe_target_health(
                        TargetGroupArn=group["TargetGroupArn"]
                    ).get("TargetHealthDescriptions", [])
                    target_count = len(health)
                    healthy_count = sum(
                        1
                        for entry in health
                        if entry.get("TargetHealth", {}).get("State") == "healthy"
                    )
                except (ClientError, BotoCoreError) as exc:
                    logger.debug("describe_target_health failed: %s", exc)
                summaries.append(
                    {
                        "name": group.get("TargetGroupName"),
                        "arn": group.get("TargetGroupArn"),
                        "protocol": group.get("Protocol"),
                        "port": group.get("Port"),
                        "target_type": group.get("TargetType"),
                        "target_count": target_count,
                        "healthy_count": healthy_count,
                    }
                )
            result[lb_arn] = summaries
        return result


@register
class ClassicLoadBalancerCollector(Collector):
    """Classic ELBs are legacy and usually cheaper to migrate than to keep."""

    key = "elb-classic"
    service = "ELB"

    def collect(self, ctx: CollectionContext, region: str) -> list[Resource]:
        client = ctx.client("elb", region)
        resources: list[Resource] = []
        for balancer in paginate(client, "describe_load_balancers", "LoadBalancerDescriptions"):
            name = balancer["LoadBalancerName"]
            instances = balancer.get("Instances", [])
            resources.append(
                Resource(
                    arn=synthesize_arn(
                        "elasticloadbalancing",
                        region,
                        ctx.account_id,
                        f"loadbalancer/{name}",
                    ),
                    resource_id=name,
                    resource_type="elb:classic",
                    service="ELB",
                    region=region,
                    account_id=ctx.account_id,
                    name=name,
                    state="active",
                    created_at=balancer.get("CreatedTime"),
                    attributes={
                        "lb_type": "classic",
                        "scheme": balancer.get("Scheme"),
                        "dns_name": balancer.get("DNSName"),
                        "vpc_id": balancer.get("VPCId"),
                        "availability_zones": balancer.get("AvailabilityZones", []),
                        "target_count": len(instances),
                        "listener_count": len(balancer.get("ListenerDescriptions", [])),
                    },
                )
            )
        return resources
