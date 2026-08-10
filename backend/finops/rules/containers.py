"""EKS clusters and node groups."""

from __future__ import annotations

from collections.abc import Iterable

from finops.model import (
    ACTION_DELETE,
    ACTION_MIGRATE,
    ACTION_REARCHITECT,
    Evidence,
    Finding,
    Remediation,
)
from finops.rules.base import Rule, RuleContext, finding_for, register
from finops.util import human_money

# Spot capacity typically lists around 70% below on-demand, but only part of a cluster
# can safely run on interruptible nodes.
SPOT_DISCOUNT = 0.65
SAFE_SPOT_FRACTION = 0.50


@register
class EmptyEksCluster(Rule):
    """A control plane with no compute attached still bills every hour."""

    id = "eks.empty_cluster"
    category = "containers"
    title = "EKS cluster with no compute"

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        for cluster in ctx.of_type("eks:cluster"):
            attributes = cluster.attributes
            if attributes.get("nodegroup_count") or attributes.get("fargate_profile_count"):
                continue

            savings = ctx.monthly_cost(cluster)
            yield finding_for(
                cluster,
                rule_id=self.id,
                title=f"EKS cluster {cluster.display_name} has no node groups or Fargate profiles",
                category=self.category,
                action=ACTION_DELETE,
                savings=savings,
                detail=(
                    "The control plane bills a flat hourly rate whether or not anything runs on "
                    "it. With no managed node groups and no Fargate profiles, this cluster has "
                    "no capacity at all. Self-managed nodes would not appear here, so confirm "
                    "before deleting."
                ),
                evidence=[
                    Evidence(label="Node groups", value="0"),
                    Evidence(label="Fargate profiles", value="0"),
                    Evidence(label="Kubernetes version", value=str(attributes.get("version"))),
                    Evidence(label="Status", value=str(cluster.state)),
                    Evidence(
                        label="Control plane cost",
                        value=f"{human_money(savings)}/month",
                    ),
                ],
                remediation=Remediation(
                    summary="Delete the cluster if it is a leftover from testing.",
                    cli=(
                        f"aws eks delete-cluster --name {cluster.resource_id} "
                        f"--region {cluster.region}"
                    ),
                    console_path=f"EKS > Clusters > {cluster.resource_id}",
                ),
                confidence="medium",
                effort="low",
                risk="high",
                rollback_possible=False,
            )


@register
class NodeGroupWithoutSpot(Rule):
    """Every node running on-demand when part of the fleet could tolerate interruption."""

    id = "eks.no_spot_capacity"
    category = "containers"
    title = "EKS node group with no Spot capacity"

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        for nodegroup in ctx.of_type("eks:nodegroup"):
            attributes = nodegroup.attributes
            if attributes.get("capacity_type") != "ON_DEMAND":
                continue
            desired = attributes.get("desired_size") or 0
            if desired < 2:
                # A single node has nowhere to fail over to when Spot reclaims it.
                continue

            current_cost = ctx.monthly_cost(nodegroup)
            if current_cost <= 0:
                continue
            savings = current_cost * SAFE_SPOT_FRACTION * SPOT_DISCOUNT

            yield finding_for(
                nodegroup,
                rule_id=self.id,
                title=(
                    f"Node group {nodegroup.display_name} runs {desired} on-demand nodes "
                    "with no Spot capacity"
                ),
                category=self.category,
                action=ACTION_MIGRATE,
                savings=savings,
                detail=(
                    f"All {desired} nodes are on-demand. Kubernetes reschedules pods when a node "
                    "disappears, which makes stateless workloads a natural fit for Spot. The "
                    f"estimate assumes only {SAFE_SPOT_FRACTION:.0%} of the group moves to Spot, "
                    "leaving on-demand capacity for anything that cannot be interrupted."
                ),
                evidence=[
                    Evidence(label="Capacity type", value="ON_DEMAND"),
                    Evidence(label="Desired nodes", value=str(desired)),
                    Evidence(
                        label="Instance types",
                        value=", ".join(attributes.get("instance_types") or []) or "unknown",
                    ),
                    Evidence(label="Cluster", value=str(attributes.get("cluster_name"))),
                    Evidence(label="Current cost", value=f"{human_money(current_cost)}/month"),
                    Evidence(
                        label="Estimate basis",
                        value=f"{SAFE_SPOT_FRACTION:.0%} of nodes at a {SPOT_DISCOUNT:.0%} "
                        "Spot discount",
                    ),
                ],
                remediation=Remediation(
                    summary=(
                        "Add a second node group with capacityType SPOT and several instance "
                        "types for diversity, then use taints or affinity to steer "
                        "interruption-tolerant workloads onto it."
                    ),
                    terraform=(
                        '# aws_eks_node_group: capacity_type = "SPOT"\n'
                        '# instance_types = ["m6i.large", "m5.large", "m5a.large"]'
                    ),
                    console_path=f"EKS > Clusters > {attributes.get('cluster_name')} > Compute",
                ),
                confidence="low",
                effort="medium",
                risk="medium",
                cost_basis="heuristic",
            )


@register
class ManySmallClusters(Rule):
    """Each cluster carries a fixed control plane charge, so many small ones add up."""

    id = "eks.cluster_sprawl"
    category = "containers"
    title = "Multiple EKS clusters with a fixed control plane cost each"

    MIN_CLUSTERS = 3

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        clusters = ctx.of_type("eks:cluster")
        if len(clusters) < self.MIN_CLUSTERS:
            return

        # Only clusters that are small enough to consolidate into namespaces.
        small = [c for c in clusters if (c.attributes.get("total_node_desired") or 0) <= 3]
        if len(small) < 2:
            return

        anchor = max(small, key=lambda c: ctx.monthly_cost(c))
        control_plane_cost = sum(ctx.monthly_cost(c) for c in small)
        # Consolidating N clusters into one leaves one control plane charge behind.
        savings = control_plane_cost * (len(small) - 1) / len(small)

        yield finding_for(
            anchor,
            rule_id=self.id,
            title=f"{len(small)} small EKS clusters each pay a separate control plane charge",
            category=self.category,
            action=ACTION_REARCHITECT,
            savings=savings,
            detail=(
                f"The account runs {len(clusters)} EKS clusters, {len(small)} of which have three "
                "nodes or fewer. Each one pays the full control plane rate regardless of size. "
                "Consolidating the small ones into a shared cluster with namespace isolation "
                "removes all but one of those charges. Keep separate clusters where you need a "
                "hard security or compliance boundary."
            ),
            evidence=[
                Evidence(label="Total clusters", value=str(len(clusters))),
                Evidence(label="Clusters with 3 or fewer nodes", value=str(len(small))),
                Evidence(
                    label="Cluster names",
                    value=", ".join(c.resource_id for c in small[:8]),
                ),
                Evidence(
                    label="Combined control plane cost",
                    value=f"{human_money(control_plane_cost)}/month",
                ),
            ],
            remediation=Remediation(
                summary=(
                    "Consolidate the small clusters into one, separating workloads with "
                    "namespaces, resource quotas, and network policies."
                ),
                console_path="EKS > Clusters",
            ),
            confidence="low",
            effort="high",
            risk="high",
            cost_basis="list_price_estimate",
        )
