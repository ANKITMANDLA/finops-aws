"""Resources that work, but are bigger or more expensive than the job requires."""

from __future__ import annotations

from collections.abc import Iterable

from finops.config import Thresholds
from finops.instances import (
    GRAVITON_DISCOUNT,
    current_generation_equivalent,
    graviton_equivalent,
    is_previous_generation,
    smaller_instance_type,
    supports_graviton,
)
from finops.model import (
    ACTION_MIGRATE,
    ACTION_RIGHTSIZE,
    ACTION_UPGRADE,
    Evidence,
    Finding,
    Remediation,
    Resource,
)
from finops.rules.base import Rule, RuleContext, finding_for, register
from finops.rules.idle import looks_idle
from finops.util import human_money

# Below this monthly cost, a resize is not worth the change management.
MIN_INTERESTING_MONTHLY_COST = 5.0


def resize_target(instance: Resource, thresholds: Thresholds) -> str | None:
    """The size this instance should drop to, or None if it is busy enough to keep.

    Shared with the Graviton rule so that one instance never attracts two rival
    recommendations for changing the same thing.
    """
    if instance.state != "running":
        return None
    cpu_p95 = instance.metrics.get("cpu_p95")
    cpu_avg = instance.metrics.get("cpu_avg")
    if cpu_p95 is None or cpu_avg is None:
        return None
    # Switching an instance off saves more than shrinking it, so where that is on the table
    # it is the better finding. It is only on the table when the instance is quiet on the
    # network too: a node at 1% CPU shifting gigabytes a day is small, not idle, and left to
    # the idle rule it would receive no recommendation from us at all.
    if looks_idle(instance, thresholds):
        return None
    if cpu_p95 >= thresholds.cpu_underutilized_percent:
        return None
    return smaller_instance_type(instance.attributes.get("instance_type") or "")


def graviton_target(instance: Resource, instance_type: str) -> str | None:
    """The ARM equivalent of a given size, if this instance could run on ARM at all."""
    if instance.attributes.get("architecture") == "arm64":
        return None
    if not supports_graviton(instance.attributes.get("platform_details") or "Linux"):
        return None
    return graviton_equivalent(instance_type)


@register
class UnderutilizedEc2Instance(Rule):
    """Peak CPU stays well under capacity, so one size down would still fit."""

    id = "ec2.underutilized_instance"
    category = "rightsizing"
    title = "Under-utilized EC2 instance"

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        for instance in ctx.of_type("ec2:instance"):
            smaller = resize_target(instance, ctx.thresholds)
            if not smaller:
                continue
            cpu_p95 = instance.metrics["cpu_p95"]
            cpu_avg = instance.metrics["cpu_avg"]
            instance_type = instance.attributes.get("instance_type") or ""
            current_cost = ctx.monthly_cost(instance)
            if current_cost < MIN_INTERESTING_MONTHLY_COST:
                continue

            platform = instance.attributes.get("platform_details") or "Linux"
            smaller_cost = ctx.pricing.ec2_instance_monthly(
                instance.region, smaller, operating_system=platform
            )
            # Halving the size halves the price in every EC2 family, so that is the
            # fallback when the exact target type has no published price.
            savings = (current_cost - smaller_cost) if smaller_cost else current_cost * 0.5

            evidence = [
                Evidence(label="p95 CPU", value=f"{cpu_p95:.1f}%"),
                Evidence(label="Average CPU", value=f"{cpu_avg:.1f}%"),
                Evidence(label="Current type", value=instance_type),
                Evidence(label="Suggested type", value=smaller),
                Evidence(label="Current cost", value=f"{human_money(current_cost)}/month"),
            ]
            detail = (
                f"95th percentile CPU is {cpu_p95:.1f}% and the average is {cpu_avg:.1f}%. "
                f"Moving to {smaller} halves the vCPU and memory while still leaving "
                "substantial headroom above the observed peak. Check memory and network "
                "requirements before resizing, since CPU alone does not tell the whole story."
            )

            # An ARM move is a second, larger prize on the same instance rather than a
            # rival recommendation, so it is described here instead of being counted twice.
            arm = graviton_target(instance, smaller)
            arm_cost = ctx.pricing.ec2_instance_monthly(instance.region, arm) if arm else None
            if arm and arm_cost is not None and smaller_cost and arm_cost < smaller_cost:
                extra = smaller_cost - arm_cost
                evidence.append(
                    Evidence(
                        label="Further on Graviton",
                        value=f"{arm} saves {human_money(extra)}/month more",
                    )
                )
                detail += (
                    f" If the workload can be rebuilt for arm64, {arm} is the cheaper landing "
                    f"place still: {human_money(extra)}/month beyond this resize. That is a "
                    "project rather than a config change, so it is not counted here."
                )

            yield finding_for(
                instance,
                rule_id=self.id,
                title=f"Resize {instance.display_name} from {instance_type} to {smaller}",
                category=self.category,
                action=ACTION_RIGHTSIZE,
                savings=savings,
                detail=detail,
                evidence=evidence,
                remediation=Remediation(
                    summary=(
                        "Stop the instance, change the instance type, and start it again. "
                        "Expect a few minutes of downtime unless it sits behind a load balancer."
                    ),
                    cli=(
                        f"aws ec2 stop-instances --instance-ids {instance.resource_id} "
                        f"--region {instance.region}\n"
                        f"aws ec2 modify-instance-attribute --instance-id {instance.resource_id} "
                        f"--instance-type {smaller} --region {instance.region}\n"
                        f"aws ec2 start-instances --instance-ids {instance.resource_id} "
                        f"--region {instance.region}"
                    ),
                    console_path=f"EC2 > Instances > {instance.resource_id} > Change instance type",
                ),
                confidence="medium",
                effort="medium",
                risk="medium",
            )


@register
class PreviousGenerationInstance(Rule):
    """Older instance families cost more per unit of work than their replacements."""

    id = "ec2.previous_generation"
    category = "rightsizing"
    title = "Previous-generation instance family"

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        for instance in ctx.of_type("ec2:instance"):
            if instance.state != "running":
                continue
            instance_type = instance.attributes.get("instance_type") or ""
            if not is_previous_generation(instance_type):
                continue
            replacement = current_generation_equivalent(instance_type)
            if not replacement:
                continue

            current_cost = ctx.monthly_cost(instance)
            if current_cost < MIN_INTERESTING_MONTHLY_COST:
                continue
            platform = instance.attributes.get("platform_details") or "Linux"
            new_cost = ctx.pricing.ec2_instance_monthly(
                instance.region, replacement, operating_system=platform
            )
            if new_cost is None:
                continue
            savings = current_cost - new_cost
            if savings <= 0:
                continue

            yield finding_for(
                instance,
                rule_id=self.id,
                title=f"Upgrade {instance.display_name} from {instance_type} to {replacement}",
                category=self.category,
                action=ACTION_UPGRADE,
                savings=savings,
                detail=(
                    f"{instance_type} belongs to a superseded family. {replacement} lists at a "
                    "lower hourly rate and delivers better performance per vCPU, so this is "
                    "usually a saving and a speed-up at once."
                ),
                evidence=[
                    Evidence(label="Current type", value=instance_type),
                    Evidence(label="Recommended type", value=replacement),
                    Evidence(label="Current cost", value=f"{human_money(current_cost)}/month"),
                    Evidence(label="Cost after upgrade", value=f"{human_money(new_cost)}/month"),
                ],
                remediation=Remediation(
                    summary=(
                        "Change the instance type during a maintenance window. Confirm the AMI "
                        "supports the newer family's Nitro requirements first."
                    ),
                    cli=(
                        f"aws ec2 modify-instance-attribute --instance-id {instance.resource_id} "
                        f"--instance-type {replacement} --region {instance.region}"
                    ),
                    console_path=f"EC2 > Instances > {instance.resource_id}",
                ),
                confidence="high",
                effort="medium",
                risk="medium",
            )


@register
class GravitonCandidate(Rule):
    """ARM instances list around 20% cheaper for workloads that can be rebuilt.

    Only instances that are the right size already. An under-utilized instance needs
    resizing first, and that finding carries the ARM option as its next step, so raising a
    second recommendation here would put two competing figures on one resource.
    """

    id = "ec2.graviton_candidate"
    category = "rightsizing"
    title = "Graviton migration candidate"

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        for instance in ctx.of_type("ec2:instance"):
            if instance.state != "running":
                continue
            instance_type = instance.attributes.get("instance_type") or ""
            replacement = graviton_target(instance, instance_type)
            if not replacement:
                continue
            current_cost = ctx.monthly_cost(instance)
            if current_cost < MIN_INTERESTING_MONTHLY_COST:
                continue
            if resize_target(instance, ctx.thresholds):
                continue
            platform = instance.attributes.get("platform_details") or "Linux"

            new_cost = ctx.pricing.ec2_instance_monthly(instance.region, replacement)
            savings = (
                current_cost - new_cost
                if new_cost is not None
                else current_cost * GRAVITON_DISCOUNT
            )
            if savings <= 0:
                continue

            yield finding_for(
                instance,
                rule_id=self.id,
                title=f"Move {instance.display_name} to Graviton ({replacement})",
                category=self.category,
                action=ACTION_MIGRATE,
                savings=savings,
                detail=(
                    f"{instance_type} runs on x86. The ARM equivalent {replacement} lists "
                    "materially cheaper for the same size. This is the highest-effort item in "
                    "this category: every binary, container image, and native dependency must "
                    "be rebuilt for arm64, so treat it as a project rather than a config change."
                ),
                evidence=[
                    Evidence(label="Current type", value=instance_type),
                    Evidence(label="Graviton equivalent", value=replacement),
                    Evidence(
                        label="Architecture", value=str(instance.attributes.get("architecture"))
                    ),
                    Evidence(label="Platform", value=platform),
                    Evidence(label="Current cost", value=f"{human_money(current_cost)}/month"),
                ],
                remediation=Remediation(
                    summary=(
                        "Build an arm64 image, validate the workload on one instance, then "
                        "migrate the fleet."
                    ),
                    console_path="EC2 > Instances",
                ),
                confidence="low",
                effort="high",
                risk="high",
            )


@register
class LambdaArmCandidate(Rule):
    """Lambda on arm64 is cheaper per GB-second than x86."""

    id = "lambda.arm_candidate"
    category = "rightsizing"
    title = "Lambda function on x86"

    ARM_DISCOUNT = 0.20

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        for function in ctx.of_type("lambda:function"):
            architectures = function.attributes.get("architectures") or ["x86_64"]
            if "arm64" in architectures:
                continue
            current_cost = ctx.monthly_cost(function)
            if current_cost < MIN_INTERESTING_MONTHLY_COST:
                continue

            savings = current_cost * self.ARM_DISCOUNT
            yield finding_for(
                function,
                rule_id=self.id,
                title=f"Switch {function.display_name} to arm64",
                category=self.category,
                action=ACTION_MIGRATE,
                savings=savings,
                detail=(
                    "Lambda charges less per GB-second on arm64 than on x86. For interpreted "
                    "runtimes with no native dependencies the switch is a single configuration "
                    "change; for compiled runtimes or functions with native modules the package "
                    "must be rebuilt."
                ),
                evidence=[
                    Evidence(label="Architecture", value=", ".join(architectures)),
                    Evidence(label="Runtime", value=str(function.attributes.get("runtime"))),
                    Evidence(label="Memory", value=f"{function.attributes.get('memory_mb')} MB"),
                    Evidence(label="Current cost", value=f"{human_money(current_cost)}/month"),
                ],
                remediation=Remediation(
                    summary="Set the function architecture to arm64 and redeploy.",
                    cli=(
                        f"aws lambda update-function-configuration --function-name "
                        f"{function.resource_id} --architectures arm64 --region {function.region}"
                    ),
                    terraform='# aws_lambda_function: architectures = ["arm64"]',
                    console_path=f"Lambda > {function.resource_id} > Configuration",
                ),
                confidence="low",
                effort="medium",
                risk="medium",
                cost_basis="heuristic",
            )


@register
class OverProvisionedDynamoTable(Rule):
    """Provisioned capacity far above what the table consumes."""

    id = "dynamodb.overprovisioned_capacity"
    category = "rightsizing"
    title = "Over-provisioned DynamoDB capacity"

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        for table in ctx.of_type("dynamodb:table"):
            if table.attributes.get("billing_mode") != "PROVISIONED":
                continue
            read_utilization = table.metrics.get("read_utilization_percent")
            write_utilization = table.metrics.get("write_utilization_percent")
            if read_utilization is None and write_utilization is None:
                continue

            worst = max(u for u in (read_utilization, write_utilization) if u is not None)
            if worst >= ctx.thresholds.dynamodb_provisioned_utilization_percent:
                continue

            current_cost = ctx.monthly_cost(table)
            if current_cost <= 0:
                continue
            # Keep 4x the observed peak as headroom; the rest is recoverable.
            retained_fraction = min(max(worst * 4 / 100.0, 0.1), 1.0)
            savings = current_cost * (1 - retained_fraction)

            yield finding_for(
                table,
                rule_id=self.id,
                title=(f"Table {table.resource_id} uses {worst:.1f}% of its provisioned capacity"),
                category=self.category,
                action=ACTION_RIGHTSIZE,
                savings=savings,
                detail=(
                    f"Peak consumed capacity is {worst:.1f}% of what is provisioned. Either "
                    "lower the provisioned units, enable auto scaling, or switch to on-demand "
                    "billing, which suits spiky or unpredictable traffic and needs no capacity "
                    "planning at all."
                ),
                evidence=[
                    Evidence(
                        label="Read utilization",
                        value=f"{read_utilization:.1f}%" if read_utilization is not None else "n/a",
                    ),
                    Evidence(
                        label="Write utilization",
                        value=f"{write_utilization:.1f}%"
                        if write_utilization is not None
                        else "n/a",
                    ),
                    Evidence(
                        label="Provisioned RCU",
                        value=str(table.attributes.get("read_capacity_units")),
                    ),
                    Evidence(
                        label="Provisioned WCU",
                        value=str(table.attributes.get("write_capacity_units")),
                    ),
                    Evidence(label="Current cost", value=f"{human_money(current_cost)}/month"),
                ],
                remediation=Remediation(
                    summary=(
                        "Switch to on-demand billing, or enable auto scaling with a target "
                        "utilization of 70%."
                    ),
                    cli=(
                        f"aws dynamodb update-table --table-name {table.resource_id} "
                        f"--billing-mode PAY_PER_REQUEST --region {table.region}"
                    ),
                    terraform='# aws_dynamodb_table: billing_mode = "PAY_PER_REQUEST"',
                    console_path=f"DynamoDB > Tables > {table.resource_id} > Additional settings",
                ),
                confidence="medium",
                effort="low",
                risk="medium",
            )
