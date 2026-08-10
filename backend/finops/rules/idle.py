"""Resources that are running and billing but doing no useful work."""

from __future__ import annotations

from collections.abc import Iterable

from finops.model import ACTION_STOP, ACTION_TERMINATE, Evidence, Finding, Remediation
from finops.rules.base import Rule, RuleContext, finding_for, register
from finops.util import human_money

# An instance needs to have existed for a while before "it has been quiet" means
# anything; a freshly launched host is quiet because it is still being set up.
MIN_AGE_DAYS = 3


@register
class IdleEc2Instance(Rule):
    """Running instances with almost no CPU and almost no network traffic."""

    id = "ec2.idle_instance"
    category = "idle"
    title = "Idle EC2 instance"

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        thresholds = ctx.thresholds
        for instance in ctx.of_type("ec2:instance"):
            if instance.state != "running":
                continue
            age = ctx.age_days(instance)
            if age is not None and age < MIN_AGE_DAYS:
                continue

            cpu_avg = instance.metrics.get("cpu_avg")
            cpu_max = instance.metrics.get("cpu_max")
            network = instance.metrics.get("network_bytes_per_day")
            # Without utilization data there is no evidence, so no finding.
            if cpu_avg is None or network is None:
                continue
            if cpu_avg >= thresholds.cpu_idle_percent:
                continue
            if network >= thresholds.network_idle_bytes_per_day:
                continue

            savings = ctx.monthly_cost(instance)
            instance_type = instance.attributes.get("instance_type", "unknown")
            yield finding_for(
                instance,
                rule_id=self.id,
                title=f"Idle EC2 instance {instance.display_name} ({instance_type})",
                category=self.category,
                action=ACTION_STOP,
                savings=savings,
                detail=(
                    f"This instance averaged {cpu_avg:.1f}% CPU and moved "
                    f"{network / 1024 / 1024:.1f} MB of network traffic per day. It appears to "
                    "be doing no work, but it is billing for compute the whole time."
                ),
                evidence=[
                    Evidence(label="Average CPU", value=f"{cpu_avg:.2f}%"),
                    Evidence(
                        label="Peak CPU", value=f"{cpu_max:.2f}%" if cpu_max is not None else "n/a"
                    ),
                    Evidence(label="Network per day", value=f"{network / 1024 / 1024:.2f} MB"),
                    Evidence(label="Instance type", value=instance_type),
                    Evidence(
                        label="Running for",
                        value=f"{age:.0f} days" if age is not None else "unknown",
                    ),
                ],
                remediation=Remediation(
                    summary=(
                        "Confirm ownership, then stop the instance. Stopping is reversible and "
                        "keeps the root volume; terminate once you are confident it is unused."
                    ),
                    cli=f"aws ec2 stop-instances --instance-ids {instance.resource_id} "
                    f"--region {instance.region}",
                    console_path=f"EC2 > Instances > {instance.resource_id}",
                ),
                confidence="high" if age and age > 14 else "medium",
                effort="low",
                risk="medium",
                rollback_possible=True,
            )


@register
class StoppedInstanceStillPayingForStorage(Rule):
    """A stopped instance bills nothing for compute, but its volumes keep charging."""

    id = "ec2.stopped_instance_storage"
    category = "idle"
    title = "Stopped EC2 instance still paying for EBS"

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        volumes_by_id = {v.resource_id: v for v in ctx.of_type("ebs:volume")}

        for instance in ctx.of_type("ec2:instance"):
            if instance.state != "stopped":
                continue
            volume_ids = instance.attributes.get("attached_volume_ids") or []
            attached = [volumes_by_id[vid] for vid in volume_ids if vid in volumes_by_id]
            storage_cost = sum(ctx.monthly_cost(volume) for volume in attached)
            if storage_cost <= 0:
                continue

            total_gb = sum(v.attributes.get("size_gb") or 0 for v in attached)
            stopped_reason = instance.attributes.get("state_transition_reason") or "unknown"
            yield finding_for(
                instance,
                rule_id=self.id,
                title=(
                    f"Stopped instance {instance.display_name} still pays "
                    f"{human_money(storage_cost)}/month for storage"
                ),
                category=self.category,
                action=ACTION_TERMINATE,
                savings=storage_cost,
                detail=(
                    f"The instance is stopped so it costs nothing to run, but its "
                    f"{len(attached)} attached volume(s) totalling {total_gb} GB continue to "
                    "bill. If the machine is not coming back, snapshot what you need and "
                    "terminate it."
                ),
                evidence=[
                    Evidence(label="Instance state", value="stopped"),
                    Evidence(label="Stopped because", value=stopped_reason),
                    Evidence(label="Attached volumes", value=str(len(attached))),
                    Evidence(label="Total storage", value=f"{total_gb} GB"),
                    Evidence(label="Monthly storage cost", value=f"{human_money(storage_cost)}"),
                ],
                remediation=Remediation(
                    summary=(
                        "Take a final snapshot if the data matters, then terminate the instance "
                        "so the volumes are released."
                    ),
                    cli=(
                        f"aws ec2 terminate-instances --instance-ids {instance.resource_id} "
                        f"--region {instance.region}"
                    ),
                    console_path=f"EC2 > Instances > {instance.resource_id}",
                ),
                confidence="high",
                effort="low",
                risk="high",
                rollback_possible=False,
            )


@register
class UnusedProvisionedConcurrency(Rule):
    """Provisioned concurrency bills continuously whether or not the function runs."""

    id = "lambda.unused_provisioned_concurrency"
    category = "idle"
    title = "Lambda provisioned concurrency with little traffic"

    # Below this many invocations a month, keeping capacity warm is hard to justify.
    LOW_TRAFFIC_INVOCATIONS = 10_000

    def evaluate(self, ctx: RuleContext) -> Iterable[Finding]:
        for function in ctx.of_type("lambda:function"):
            provisioned = function.attributes.get("provisioned_concurrency") or 0
            if provisioned <= 0:
                continue
            invocations = function.metrics.get("invocations_per_month")
            if invocations is None or invocations >= self.LOW_TRAFFIC_INVOCATIONS:
                continue

            savings = ctx.monthly_cost(function)
            yield finding_for(
                function,
                rule_id=self.id,
                title=(
                    f"{function.display_name} keeps {provisioned:g} provisioned "
                    "concurrency for very little traffic"
                ),
                category=self.category,
                action=ACTION_STOP,
                savings=savings,
                detail=(
                    f"Provisioned concurrency of {provisioned:g} bills around the clock, but the "
                    f"function was invoked about {invocations:,.0f} times a month. Unless cold "
                    "starts are business critical, on-demand concurrency is far cheaper."
                ),
                evidence=[
                    Evidence(label="Provisioned concurrency", value=f"{provisioned:g}"),
                    Evidence(label="Invocations per month", value=f"{invocations:,.0f}"),
                    Evidence(label="Memory", value=f"{function.attributes.get('memory_mb')} MB"),
                ],
                remediation=Remediation(
                    summary="Remove provisioned concurrency and rely on on-demand scaling.",
                    cli=(
                        "aws lambda delete-provisioned-concurrency-config "
                        f"--function-name {function.resource_id} --qualifier <alias> "
                        f"--region {function.region}"
                    ),
                    console_path=f"Lambda > {function.resource_id} > Configuration > Concurrency",
                ),
                confidence="medium",
                effort="low",
                risk="medium",
            )
