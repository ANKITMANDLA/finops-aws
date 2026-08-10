"""Networking charges that accrue whether or not traffic flows."""

from __future__ import annotations

from collections.abc import Iterable

from finops.model import ACTION_DELETE, ACTION_RELEASE, Evidence, Finding, Remediation
from finops.rules.base import Rule, RuleContext, finding_for, register
from finops.util import human_money


@register
class UnassociatedElasticIp(Rule):
    """Every public IPv4 address bills hourly; an unattached one buys nothing."""

    id = "eip.unassociated"
    category = "network"
    title = "Unassociated Elastic IP"

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        for address in ctx.of_type("ec2:elastic-ip"):
            if address.attributes.get("associated"):
                continue

            savings = ctx.monthly_cost(address)
            public_ip = address.attributes.get("public_ip", address.resource_id)
            yield finding_for(
                address,
                rule_id=self.id,
                title=f"Elastic IP {public_ip} is allocated but not attached to anything",
                category=self.category,
                action=ACTION_RELEASE,
                savings=savings,
                detail=(
                    "AWS charges for every allocated public IPv4 address. This one is not "
                    "associated with an instance or network interface, so it is pure waste "
                    "unless it is being held to preserve a DNS record."
                ),
                evidence=[
                    Evidence(label="Public IP", value=str(public_ip)),
                    Evidence(label="Association", value="none"),
                    Evidence(label="Scope", value=str(address.attributes.get("domain", "vpc"))),
                ],
                remediation=Remediation(
                    summary=(
                        "Release the address. Check first that no DNS record or allowlist "
                        "outside AWS depends on it, because the IP cannot be reclaimed."
                    ),
                    cli=(
                        f"aws ec2 release-address --allocation-id {address.resource_id} "
                        f"--region {address.region}"
                    ),
                    terraform="# Remove the aws_eip resource from your configuration.",
                    console_path="EC2 > Elastic IPs",
                ),
                confidence="high",
                effort="low",
                risk="medium",
                rollback_possible=False,
            )


@register
class IdleNatGateway(Rule):
    """A NAT Gateway costs about $32/month before a single byte is processed."""

    id = "natgw.idle"
    category = "network"
    title = "Idle NAT Gateway"

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        for gateway in ctx.of_type("ec2:nat-gateway"):
            if gateway.state != "available":
                continue
            bytes_per_day = gateway.metrics.get("nat_bytes_per_day")
            if bytes_per_day is None or bytes_per_day >= ctx.thresholds.nat_idle_bytes_per_day:
                continue

            savings = ctx.monthly_cost(gateway)
            yield finding_for(
                gateway,
                rule_id=self.id,
                title=f"NAT Gateway {gateway.resource_id} processes almost no traffic",
                category=self.category,
                action=ACTION_DELETE,
                savings=savings,
                detail=(
                    f"The gateway moved {bytes_per_day / 1024 / 1024:.2f} MB per day. The hourly "
                    "charge applies regardless of traffic, so an unused gateway costs the same "
                    "as a busy one. Check whether the private subnets behind it still need "
                    "outbound internet access, or whether a VPC endpoint would serve better."
                ),
                evidence=[
                    Evidence(
                        label="Bytes processed per day",
                        value=f"{bytes_per_day / 1024 / 1024:.2f} MB",
                    ),
                    Evidence(label="VPC", value=str(gateway.attributes.get("vpc_id"))),
                    Evidence(label="Subnet", value=str(gateway.attributes.get("subnet_id"))),
                    Evidence(
                        label="Connectivity",
                        value=str(gateway.attributes.get("connectivity_type", "public")),
                    ),
                ],
                remediation=Remediation(
                    summary=(
                        "Delete the gateway and remove the route, or replace it with VPC "
                        "endpoints if the only outbound traffic is to AWS services."
                    ),
                    cli=(
                        f"aws ec2 delete-nat-gateway --nat-gateway-id {gateway.resource_id} "
                        f"--region {gateway.region}"
                    ),
                    console_path="VPC > NAT gateways",
                ),
                confidence="medium",
                effort="medium",
                risk="high",
            )


@register
class LoadBalancerWithNoHealthyTargets(Rule):
    """A load balancer with nothing behind it cannot be serving anyone."""

    id = "elb.no_healthy_targets"
    category = "network"
    title = "Load balancer with no healthy targets"

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        for balancer in ctx.of_type("elbv2:application", "elbv2:network", "elbv2:gateway"):
            attributes = balancer.attributes
            target_count = attributes.get("target_count")
            healthy = attributes.get("healthy_target_count")
            if target_count is None or healthy is None:
                continue
            if healthy > 0:
                continue

            savings = ctx.monthly_cost(balancer)
            requests = balancer.metrics.get("requests_per_day")
            yield finding_for(
                balancer,
                rule_id=self.id,
                title=f"Load balancer {balancer.display_name} has no healthy targets",
                category=self.category,
                action=ACTION_DELETE,
                savings=savings,
                detail=(
                    f"The load balancer has {target_count} registered target(s) and none of them "
                    "are healthy, so it cannot be serving traffic. It still bills an hourly "
                    "charge plus capacity units."
                ),
                evidence=[
                    Evidence(label="Registered targets", value=str(target_count)),
                    Evidence(label="Healthy targets", value="0"),
                    Evidence(
                        label="Requests per day",
                        value=f"{requests:,.0f}" if requests is not None else "no data",
                    ),
                    Evidence(label="Type", value=str(attributes.get("lb_type"))),
                    Evidence(label="Scheme", value=str(attributes.get("scheme"))),
                ],
                remediation=Remediation(
                    summary="Delete the load balancer and its orphaned target groups.",
                    cli=(
                        "aws elbv2 delete-load-balancer --load-balancer-arn "
                        f"{balancer.arn} --region {balancer.region}"
                    ),
                    console_path=f"EC2 > Load balancers > {balancer.resource_id}",
                ),
                confidence="high",
                effort="low",
                risk="high",
            )


@register
class IdleLoadBalancer(Rule):
    """Targets are healthy, but nobody is calling."""

    id = "elb.idle"
    category = "network"
    title = "Load balancer with negligible traffic"

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        for balancer in ctx.of_type("elbv2:application", "elbv2:network", "elb:classic"):
            requests = balancer.metrics.get("requests_per_day")
            if requests is None or requests >= ctx.thresholds.elb_idle_requests_per_day:
                continue
            # Already reported by the no-healthy-targets rule, which is more specific.
            if balancer.attributes.get("healthy_target_count") == 0:
                continue

            savings = ctx.monthly_cost(balancer)
            yield finding_for(
                balancer,
                rule_id=self.id,
                title=(
                    f"Load balancer {balancer.display_name} serves about "
                    f"{requests:,.0f} requests a day"
                ),
                category=self.category,
                action=ACTION_DELETE,
                savings=savings,
                detail=(
                    f"At {requests:,.0f} requests per day this load balancer costs "
                    f"{human_money(savings)} a month to answer very little. Consider folding the "
                    "workload behind an existing load balancer using host or path routing."
                ),
                evidence=[
                    Evidence(label="Requests per day", value=f"{requests:,.0f}"),
                    Evidence(
                        label="Healthy targets",
                        value=str(balancer.attributes.get("healthy_target_count", "unknown")),
                    ),
                    Evidence(label="Type", value=str(balancer.attributes.get("lb_type"))),
                ],
                remediation=Remediation(
                    summary=(
                        "Consolidate onto a shared load balancer with a host-based listener "
                        "rule, then delete this one."
                    ),
                    console_path=f"EC2 > Load balancers > {balancer.resource_id}",
                ),
                confidence="medium",
                effort="medium",
                risk="medium",
            )
