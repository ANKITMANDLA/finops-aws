"""Recommendations produced by AWS itself.

Three sources, in decreasing order of usefulness:

* **Compute Optimizer** analyses CloudWatch history and proposes concrete instance,
  volume, and memory sizes with a projected saving.
* **Cost Optimization Hub** aggregates every AWS optimization recommendation, already
  de-duplicated and ranked, including commitment purchases.
* **Trusted Advisor** cost checks, available only with Business or Enterprise Support.

All three are normalized onto the same :class:`~finops.model.Finding` type as our own
rules, using the shared action vocabulary so overlapping advice merges instead of being
counted twice.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from finops.aws.collectors.base import synthesize_arn
from finops.aws.errors import NoteCollector, graceful
from finops.aws.session import AwsContext
from finops.model import (
    ACTION_DELETE,
    ACTION_MIGRATE,
    ACTION_MODIFY_STORAGE,
    ACTION_PURCHASE_COMMITMENT,
    ACTION_RELEASE,
    ACTION_RIGHTSIZE,
    ACTION_STOP,
    ACTION_UPGRADE,
    Evidence,
    Finding,
    FindingCategory,
    Remediation,
    make_finding_id,
)

logger = logging.getLogger(__name__)

# Cost Optimization Hub action types mapped onto our shared vocabulary.
_HUB_ACTION_MAP: dict[str, tuple[str, FindingCategory]] = {
    "Rightsize": (ACTION_RIGHTSIZE, "rightsizing"),
    "Stop": (ACTION_STOP, "idle"),
    "Delete": (ACTION_DELETE, "idle"),
    "Upgrade": (ACTION_UPGRADE, "rightsizing"),
    "PurchaseSavingsPlans": (ACTION_PURCHASE_COMMITMENT, "commitments"),
    "PurchaseReservedInstances": (ACTION_PURCHASE_COMMITMENT, "commitments"),
    "MigrateToGraviton": (ACTION_MIGRATE, "rightsizing"),
    "ScaleIn": (ACTION_RIGHTSIZE, "rightsizing"),
}

_HUB_EFFORT_MAP = {
    "VeryLow": "low",
    "Low": "low",
    "Medium": "medium",
    "High": "high",
    "VeryHigh": "high",
}


class NativeRecommendations:
    """Collects and normalizes AWS's own optimization recommendations."""

    def __init__(self, aws: AwsContext, notes: NoteCollector | None = None) -> None:
        self.aws = aws
        self.notes = notes or NoteCollector()

    def collect(self, regions: Sequence[str] | None = None) -> list[Finding]:
        target_regions = list(regions) if regions else self.aws.regions
        findings: list[Finding] = []

        findings.extend(self.cost_optimization_hub())
        findings.extend(self.trusted_advisor())

        workers = min(self.aws.settings.max_workers, max(len(target_regions), 1))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="native") as pool:
            for region_findings in pool.map(self.compute_optimizer, target_regions):
                findings.extend(region_findings)
        return findings

    # ------------------------------------------------------- compute optimizer

    def compute_optimizer(self, region: str) -> list[Finding]:
        findings: list[Finding] = []
        client = self.aws.client("compute-optimizer", region)

        with graceful(self.notes, "compute-optimizer", region=region):
            status = client.get_enrollment_status().get("status")
            if status != "Active":
                self.notes.add(
                    "compute-optimizer",
                    "not_enrolled",
                    f"Compute Optimizer enrollment status is {status or 'unknown'}.",
                    region=region,
                    remedy="Opt in at https://console.aws.amazon.com/compute-optimizer/ "
                    "(free). Recommendations appear roughly 12 hours later.",
                )
                return findings

        collectors = (
            ("ec2", self._ec2_recommendations),
            ("ebs", self._ebs_recommendations),
            ("lambda", self._lambda_recommendations),
            ("asg", self._asg_recommendations),
            ("idle", self._idle_recommendations),
        )
        for name, handler in collectors:
            with graceful(self.notes, f"compute-optimizer:{name}", region=region):
                findings.extend(handler(client, region))
        return findings

    def _ec2_recommendations(self, client, region: str) -> list[Finding]:
        findings: list[Finding] = []
        paginator_kwargs: dict[str, Any] = {}
        while True:
            response = client.get_ec2_instance_recommendations(**paginator_kwargs)
            for item in response.get("instanceRecommendations", []):
                finding = self._from_compute_optimizer_option(
                    arn=item.get("instanceArn", ""),
                    region=region,
                    service="EC2",
                    resource_type="ec2:instance",
                    resource_id=_arn_tail(item.get("instanceArn", "")),
                    name=item.get("instanceName"),
                    finding_label=item.get("finding"),
                    current=item.get("currentInstanceType"),
                    options=item.get("recommendationOptions", []),
                    option_key="instanceType",
                    reasons=item.get("findingReasonCodes", []),
                    action=ACTION_RIGHTSIZE,
                    category="rightsizing",
                    tags=_tag_list_to_dict(item.get("tags")),
                )
                if finding:
                    findings.append(finding)
            token = response.get("nextToken")
            if not token:
                break
            paginator_kwargs = {"nextToken": token}
        return findings

    def _ebs_recommendations(self, client, region: str) -> list[Finding]:
        findings: list[Finding] = []
        paginator_kwargs: dict[str, Any] = {}
        while True:
            response = client.get_ebs_volume_recommendations(**paginator_kwargs)
            for item in response.get("volumeRecommendations", []):
                current = (item.get("currentConfiguration") or {}).get("volumeType")
                options = [
                    {
                        "configuration": option.get("configuration", {}),
                        "savingsOpportunity": option.get("savingsOpportunity", {}),
                        "volumeType": (option.get("configuration") or {}).get("volumeType"),
                        "rank": option.get("rank"),
                    }
                    for option in item.get("volumeRecommendationOptions", [])
                ]
                finding = self._from_compute_optimizer_option(
                    arn=item.get("volumeArn", ""),
                    region=region,
                    service="EBS",
                    resource_type="ebs:volume",
                    resource_id=_arn_tail(item.get("volumeArn", "")),
                    name=None,
                    finding_label=item.get("finding"),
                    current=current,
                    options=options,
                    option_key="volumeType",
                    reasons=[],
                    action=ACTION_MODIFY_STORAGE,
                    category="storage",
                    tags=_tag_list_to_dict(item.get("tags")),
                )
                if finding:
                    findings.append(finding)
            token = response.get("nextToken")
            if not token:
                break
            paginator_kwargs = {"nextToken": token}
        return findings

    def _lambda_recommendations(self, client, region: str) -> list[Finding]:
        findings: list[Finding] = []
        paginator_kwargs: dict[str, Any] = {}
        while True:
            response = client.get_lambda_function_recommendations(**paginator_kwargs)
            for item in response.get("lambdaFunctionRecommendations", []):
                options = [
                    {
                        "memorySize": option.get("memorySize"),
                        "savingsOpportunity": option.get("savingsOpportunity", {}),
                        "rank": option.get("rank"),
                    }
                    for option in item.get("memorySizeRecommendationOptions", [])
                ]
                finding = self._from_compute_optimizer_option(
                    arn=item.get("functionArn", ""),
                    region=region,
                    service="Lambda",
                    resource_type="lambda:function",
                    resource_id=item.get("functionName"),
                    name=item.get("functionName"),
                    finding_label=item.get("finding"),
                    current=f"{item.get('currentMemorySize')} MB",
                    options=options,
                    option_key="memorySize",
                    reasons=item.get("findingReasonCodes", []),
                    action=ACTION_RIGHTSIZE,
                    category="rightsizing",
                    tags=_tag_list_to_dict(item.get("tags")),
                )
                if finding:
                    findings.append(finding)
            token = response.get("nextToken")
            if not token:
                break
            paginator_kwargs = {"nextToken": token}
        return findings

    def _asg_recommendations(self, client, region: str) -> list[Finding]:
        findings: list[Finding] = []
        response = client.get_auto_scaling_group_recommendations()
        for item in response.get("autoScalingGroupRecommendations", []):
            current = (item.get("currentConfiguration") or {}).get("instanceType")
            options = [
                {
                    "instanceType": (option.get("configuration") or {}).get("instanceType"),
                    "savingsOpportunity": option.get("savingsOpportunity", {}),
                    "rank": option.get("rank"),
                }
                for option in item.get("recommendationOptions", [])
            ]
            finding = self._from_compute_optimizer_option(
                arn=item.get("autoScalingGroupArn", ""),
                region=region,
                service="AutoScaling",
                resource_type="autoscaling:group",
                resource_id=item.get("autoScalingGroupName"),
                name=item.get("autoScalingGroupName"),
                finding_label=item.get("finding"),
                current=current,
                options=options,
                option_key="instanceType",
                reasons=[],
                action=ACTION_RIGHTSIZE,
                category="rightsizing",
                tags={},
            )
            if finding:
                findings.append(finding)
        return findings

    def _idle_recommendations(self, client, region: str) -> list[Finding]:
        """Compute Optimizer's dedicated idle-resource finding set."""
        findings: list[Finding] = []
        response = client.get_idle_recommendations()
        for item in response.get("idleRecommendations", []):
            arn = item.get("resourceArn", "")
            savings = _savings_value(item.get("savingsOpportunity", {}))
            if savings <= 0:
                continue
            raw_type = str(item.get("resourceType") or "resource")
            description = item.get("findingDescription") or ""
            evidence = [Evidence(label="Compute Optimizer finding", value=str(item.get("finding")))]
            if description:
                evidence.append(Evidence(label="Reason", value=description))
            findings.append(
                Finding(
                    id=make_finding_id(ACTION_STOP, arn),
                    rule_id="compute-optimizer.idle",
                    title=f"Idle {_humanize(raw_type)}: {_arn_tail(arn)}",
                    category="idle",
                    action_type=ACTION_STOP,
                    service=_service_for_resource_type(raw_type),
                    source="compute-optimizer",
                    resource_arn=arn,
                    resource_id=_arn_tail(arn),
                    resource_type=raw_type,
                    region=region,
                    estimated_monthly_savings=savings,
                    confidence="high",
                    implementation_effort="low",
                    risk="medium",
                    cost_basis="aws_recommendation",
                    rollback_possible=True,
                    detail=description
                    or f"Compute Optimizer classified this resource as {item.get('finding')}.",
                    evidence=evidence,
                    remediation=Remediation(
                        summary="Confirm the resource is unused, then stop or delete it.",
                        console_path="Compute Optimizer > Idle resources",
                    ),
                    tags=_tag_list_to_dict(item.get("tags")),
                )
            )
        return findings

    def _from_compute_optimizer_option(
        self,
        *,
        arn: str,
        region: str,
        service: str,
        resource_type: str,
        resource_id: str | None,
        name: str | None,
        finding_label: str | None,
        current: Any,
        options: Sequence[dict[str, Any]],
        option_key: str,
        reasons: Sequence[str],
        action: str,
        category: FindingCategory,
        tags: dict[str, str],
    ) -> Finding | None:
        """Turn the best-ranked recommendation option into a Finding."""
        if finding_label in ("Optimized", "NotAvailable", None):
            return None

        best = max(
            options,
            key=lambda option: _savings_value(option.get("savingsOpportunity", {})),
            default=None,
        )
        if best is None:
            return None
        savings = _savings_value(best.get("savingsOpportunity", {}))
        if savings <= 0:
            return None

        target = best.get(option_key)
        if option_key == "memorySize" and target:
            target = f"{target} MB"

        evidence = [
            Evidence(label="Compute Optimizer finding", value=str(finding_label)),
            Evidence(label="Current", value=str(current)),
            Evidence(label="Recommended", value=str(target)),
        ]
        percentage = (best.get("savingsOpportunity") or {}).get("savingsOpportunityPercentage")
        if percentage:
            evidence.append(Evidence(label="Savings opportunity", value=f"{percentage:.1f}%"))
        evidence.extend(Evidence(label="Reason", value=_humanize(reason)) for reason in reasons[:4])

        return Finding(
            id=make_finding_id(action, arn or resource_id or ""),
            rule_id=f"compute-optimizer.{resource_type.replace(':', '_')}",
            title=f"Resize {name or resource_id} from {current} to {target}",
            category=category,
            action_type=action,
            service=service,
            source="compute-optimizer",
            resource_arn=arn or None,
            resource_id=resource_id,
            resource_type=resource_type,
            region=region,
            estimated_monthly_savings=savings,
            confidence="high",
            implementation_effort="medium" if action == ACTION_RIGHTSIZE else "low",
            risk="medium",
            cost_basis="aws_recommendation",
            rollback_possible=True,
            detail=(
                f"Compute Optimizer analysed CloudWatch history and classified this resource "
                f"as {finding_label}. The best-ranked option is {target}."
            ),
            evidence=evidence,
            remediation=Remediation(
                summary=f"Change the configuration to {target} during a maintenance window.",
                console_path="Compute Optimizer > Recommendations",
            ),
            tags=tags,
        )

    # --------------------------------------------------- cost optimization hub

    def cost_optimization_hub(self) -> list[Finding]:
        findings: list[Finding] = []
        client = self.aws.client("cost-optimization-hub")

        with graceful(self.notes, "cost-optimization-hub"):
            statuses = client.list_enrollment_statuses().get("items", [])
            active = any(entry.get("status") == "Active" for entry in statuses)
            if not active:
                self.notes.add(
                    "cost-optimization-hub",
                    "not_enrolled",
                    "Cost Optimization Hub is not enabled for this account.",
                    remedy="Enable it under Billing and Cost Management > Cost Optimization Hub "
                    "(free). Recommendations appear within 24 hours.",
                )
                return findings

        with graceful(self.notes, "cost-optimization-hub:ListRecommendations"):
            next_token: str | None = None
            while True:
                kwargs: dict[str, Any] = {"includeAllRecommendations": True, "maxResults": 100}
                if next_token:
                    kwargs["nextToken"] = next_token
                response = client.list_recommendations(**kwargs)
                for item in response.get("items", []):
                    finding = self._from_hub_item(item)
                    if finding:
                        findings.append(finding)
                next_token = response.get("nextToken")
                if not next_token:
                    break
        return findings

    def _from_hub_item(self, item: dict[str, Any]) -> Finding | None:
        savings = float(item.get("estimatedMonthlySavings") or 0.0)
        if savings <= 0:
            return None

        raw_action = str(item.get("actionType", ""))
        action, category = _HUB_ACTION_MAP.get(raw_action, (raw_action.lower(), "rightsizing"))
        arn = item.get("resourceArn") or ""
        resource_id = item.get("resourceId") or _arn_tail(arn)
        current = item.get("currentResourceSummary") or item.get("currentResourceType")
        recommended = item.get("recommendedResourceSummary") or item.get("recommendedResourceType")
        # Commitment purchases are account-wide, so they need a synthetic identity.
        key = arn or resource_id or f"account:{raw_action}:{recommended}"

        evidence = [
            Evidence(label="Current", value=str(current)),
            Evidence(label="Recommended", value=str(recommended)),
        ]
        if item.get("estimatedSavingsPercentage"):
            evidence.append(
                Evidence(
                    label="Savings",
                    value=f"{float(item['estimatedSavingsPercentage']):.1f}% of current cost",
                )
            )
        if item.get("estimatedMonthlyCost") is not None:
            evidence.append(
                Evidence(
                    label="Estimated cost after change",
                    value=f"${float(item['estimatedMonthlyCost']):,.2f}/month",
                )
            )
        if item.get("restartNeeded"):
            evidence.append(Evidence(label="Restart required", value="Yes"))

        return Finding(
            id=make_finding_id(action, key),
            rule_id=f"cost-optimization-hub.{raw_action or 'recommendation'}",
            title=f"{_humanize(raw_action)}: {resource_id or recommended}",
            category=category,
            action_type=action,
            service=_service_for_resource_type(str(item.get("currentResourceType", ""))),
            source="cost-optimization-hub",
            resource_arn=arn or None,
            resource_id=resource_id,
            resource_type=item.get("currentResourceType"),
            region=item.get("region"),
            estimated_monthly_savings=savings,
            currency=item.get("currencyCode", "USD"),
            confidence="high",
            implementation_effort=_HUB_EFFORT_MAP.get(  # type: ignore[arg-type]
                str(item.get("implementationEffort")), "medium"
            ),
            risk="high" if item.get("restartNeeded") else "low",
            cost_basis="aws_recommendation",
            rollback_possible=bool(item.get("rollbackPossible", True)),
            detail=(
                "Reported by Cost Optimization Hub, which aggregates and de-duplicates AWS's "
                "own optimization recommendations."
            ),
            evidence=evidence,
            remediation=Remediation(
                summary=f"Apply the recommended change: {recommended}.",
                console_path="Billing and Cost Management > Cost Optimization Hub",
            ),
            tags=_tag_list_to_dict(item.get("tags")),
        )

    # ---------------------------------------------------------- trusted advisor

    def trusted_advisor(self) -> list[Finding]:
        findings: list[Finding] = []
        client = self.aws.client("support")

        with graceful(self.notes, "trusted-advisor"):
            checks = [
                check
                for check in client.describe_trusted_advisor_checks(language="en").get("checks", [])
                if check.get("category") == "cost_optimizing"
            ]
            for check in checks:
                findings.extend(self._trusted_advisor_check(client, check))

        if self.notes.has_problem("trusted-advisor"):
            self.notes.add(
                "trusted-advisor",
                "unavailable",
                "Trusted Advisor cost checks need a Business or Enterprise Support plan.",
                remedy="Skip this source, or upgrade the support plan. Compute Optimizer and "
                "Cost Optimization Hub cover most of the same ground for free.",
            )
        return findings

    def _trusted_advisor_check(self, client, check: dict[str, Any]) -> list[Finding]:
        findings: list[Finding] = []
        check_id = check.get("id")
        check_name = check.get("name", "Trusted Advisor check")
        metadata_columns = check.get("metadata", [])

        result = client.describe_trusted_advisor_check_result(checkId=check_id, language="en")
        detail = result.get("result", {})
        if detail.get("status") == "ok":
            return findings

        action, category = _trusted_advisor_action(check_name)
        for flagged in detail.get("flaggedResources", []):
            if flagged.get("isSuppressed"):
                continue
            metadata = flagged.get("metadata", [])
            fields = dict(zip(metadata_columns, metadata, strict=False))
            savings = _extract_savings(fields)
            # Trusted Advisor's own resourceId is an opaque check-scoped hash. The real
            # AWS identifier is in the metadata columns, and only that can be matched
            # against the inventory or against another source's view of the same issue.
            resource_id = _first_identifier(fields) or flagged.get("resourceId")
            region = flagged.get("region")
            arn = _trusted_advisor_arn(resource_id, region, self.aws.account_id)
            findings.append(
                Finding(
                    id=make_finding_id(action, arn or f"ta:{check_id}:{resource_id}"),
                    rule_id=f"trusted-advisor.{check_id}",
                    title=f"{check_name}: {resource_id}",
                    category=category,
                    action_type=action,
                    service=_service_from_check_name(check_name),
                    source="trusted-advisor",
                    resource_arn=arn,
                    resource_id=resource_id,
                    region=region,
                    estimated_monthly_savings=savings,
                    confidence="medium",
                    implementation_effort="medium",
                    risk="medium",
                    cost_basis="aws_recommendation",
                    detail=check.get("description", "")[:600],
                    evidence=[
                        Evidence(label=str(column), value=str(value))
                        for column, value in fields.items()
                        if value
                    ][:6],
                    remediation=Remediation(
                        summary="Review the flagged resource in Trusted Advisor and act on it.",
                        console_path=f"Trusted Advisor > Cost Optimization > {check_name}",
                    ),
                )
            )
        return findings


# ------------------------------------------------------------------- helpers


def _savings_value(savings_opportunity: dict[str, Any]) -> float:
    estimated = savings_opportunity.get("estimatedMonthlySavings") or {}
    try:
        return round(float(estimated.get("value") or 0.0), 2)
    except (TypeError, ValueError):
        return 0.0


def _arn_tail(arn: str) -> str | None:
    if not arn:
        return None
    tail = arn.rsplit("/", 1)[-1] if "/" in arn else arn.rsplit(":", 1)[-1]
    return tail or None


def _tag_list_to_dict(tags: Any) -> dict[str, str]:
    if not tags:
        return {}
    if isinstance(tags, dict):
        return {str(k): str(v) for k, v in tags.items()}
    result: dict[str, str] = {}
    for tag in tags:
        key = tag.get("key") or tag.get("Key")
        if key:
            result[str(key)] = str(tag.get("value") or tag.get("Value") or "")
    return result


# Split CamelCase without breaking up acronyms: "CPUOverprovisioned" becomes
# "CPU Overprovisioned" rather than "C P U Overprovisioned".
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _humanize(value: str) -> str:
    """Turn CamelCase and SNAKE_CASE API constants into readable text."""
    spaced = _CAMEL_BOUNDARY.sub(" ", str(value).replace("_", " "))
    collapsed = " ".join(spaced.split())
    if not collapsed:
        return ""
    # Capitalize only the first word so acronyms keep their casing.
    return collapsed[0].upper() + collapsed[1:]


_RESOURCE_TYPE_SERVICE = {
    "ec2instance": "EC2",
    "ec2:instance": "EC2",
    "ebsvolume": "EBS",
    "ebs:volume": "EBS",
    "lambdafunction": "Lambda",
    "lambda:function": "Lambda",
    "ecsservice": "ECS",
    "rdsdbinstance": "RDS",
    "autoscalinggroup": "AutoScaling",
    "computesavingsplans": "Savings Plans",
    "ec2instancesavingsplans": "Savings Plans",
    "sagemakersavingsplans": "Savings Plans",
    "ec2reservedinstances": "Reserved Instances",
    "rdsreservedinstances": "Reserved Instances",
}


def _service_for_resource_type(resource_type: str) -> str:
    key = resource_type.replace(" ", "").lower()
    return _RESOURCE_TYPE_SERVICE.get(key, resource_type or "AWS")


def _service_from_check_name(check_name: str) -> str:
    lowered = check_name.lower()
    for keyword, service in (
        ("ec2", "EC2"),
        ("ebs", "EBS"),
        ("elastic ip", "VPC"),
        ("load balancer", "ELB"),
        ("rds", "RDS"),
        ("redshift", "Redshift"),
        ("route 53", "Route 53"),
        ("s3", "S3"),
        ("lambda", "Lambda"),
    ):
        if keyword in lowered:
            return service
    return "AWS"


_MONEY_PATTERN = re.compile(r"\$?\s*([0-9][0-9,]*\.?[0-9]*)")


def _extract_savings(fields: dict[str, str]) -> float:
    """Trusted Advisor reports savings in a differently-named column per check."""
    for column, value in fields.items():
        if "saving" not in column.lower() or not value:
            continue
        match = _MONEY_PATTERN.search(str(value))
        if match:
            try:
                return round(float(match.group(1).replace(",", "")), 2)
            except ValueError:
                continue
    return 0.0


def _first_identifier(fields: dict[str, str]) -> str:
    """Pick the column that identifies the resource, preferring ids over display names."""
    for tokens in (("id", "arn"), ("name",)):
        for column, value in fields.items():
            lowered = column.lower()
            words = re.split(r"[\s_]+", lowered)
            if value and any(token in words or lowered.endswith(token) for token in tokens):
                return str(value)
    return next((str(v) for v in fields.values() if v), "unknown")


# Trusted Advisor check names, keyed by a distinctive phrase, mapped onto the action the
# check is really asking for. Without this every check reads as "delete, governance".
_TA_ACTIONS: tuple[tuple[str, str, str], ...] = (
    ("low utilization", ACTION_RIGHTSIZE, "rightsizing"),
    ("underutilized", ACTION_RIGHTSIZE, "rightsizing"),
    ("idle", ACTION_STOP, "idle"),
    ("unassociated", ACTION_RELEASE, "network"),
    ("unattached", ACTION_DELETE, "storage"),
    ("without", ACTION_DELETE, "storage"),
    ("reserved instance", ACTION_PURCHASE_COMMITMENT, "commitments"),
    ("savings plan", ACTION_PURCHASE_COMMITMENT, "commitments"),
    ("lease expiration", ACTION_PURCHASE_COMMITMENT, "commitments"),
)


def _trusted_advisor_action(check_name: str) -> tuple[str, str]:
    lowered = check_name.lower()
    for phrase, action, category in _TA_ACTIONS:
        if phrase in lowered:
            return action, category
    return ACTION_DELETE, "governance"


# Identifier shapes we can turn back into an ARN, so a Trusted Advisor finding lands on
# the same resource our own rules and Compute Optimizer already know about.
_TA_ARN_PATHS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"^i-[0-9a-f]{8,}$"), "ec2", "instance/{id}"),
    (re.compile(r"^vol-[0-9a-f]{8,}$"), "ec2", "volume/{id}"),
    (re.compile(r"^eipalloc-[0-9a-f]{8,}$"), "ec2", "elastic-ip/{id}"),
)


def _trusted_advisor_arn(
    resource_id: str | None, region: str | None, account_id: str
) -> str | None:
    if not resource_id or not region or account_id == "unknown":
        return None
    if resource_id.startswith("arn:"):
        return resource_id
    for pattern, service, path in _TA_ARN_PATHS:
        if pattern.match(resource_id):
            return synthesize_arn(service, region, account_id, path.format(id=resource_id))
    return None
