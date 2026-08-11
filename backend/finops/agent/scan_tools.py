"""The scan itself, exposed as tools.

AWS's MCP servers know everything about AWS and nothing about this account. These tools
are the other half: they let the model look up what was actually found here, so an answer
can cite a specific volume or finding instead of generalizing. Everything is read-only and
served from the stored scan, so asking a question never touches AWS.
"""

from __future__ import annotations

import json
from typing import Any

from finops.agent.types import ToolSpec
from finops.store import ScanStore
from finops.util import round_money

SOURCE = "finops"
MAX_ROWS = 50

TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="finops_cost_breakdown",
        source=SOURCE,
        description=(
            "Spend and identified savings for this account, broken down by service, "
            "region, usage type, savings category, and effort. Start here for anything "
            "about totals or where the money goes."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "dimension": {
                    "type": "string",
                    "enum": ["service", "region", "usage_type", "category", "effort", "all"],
                    "description": "Which breakdown to return. Defaults to all.",
                }
            },
        },
    ),
    ToolSpec(
        name="finops_search_findings",
        source=SOURCE,
        description=(
            "Search the cost findings this scan produced: idle resources, rightsizing, "
            "storage, network, commitments, governance. Returns savings, effort, risk, "
            "and the finding id needed by finops_get_finding."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Free text over title and resource."},
                "service": {"type": "string", "description": "e.g. EC2, EBS, RDS, S3."},
                "category": {
                    "type": "string",
                    "description": "idle, rightsizing, storage, network, commitments, governance.",
                },
                "region": {"type": "string"},
                "min_savings": {
                    "type": "number",
                    "description": "Only findings worth at least this much per month.",
                },
                "limit": {"type": "integer", "description": f"Default 20, max {MAX_ROWS}."},
            },
        },
    ),
    ToolSpec(
        name="finops_get_finding",
        source=SOURCE,
        description=(
            "Everything about one finding: the evidence behind it, how the saving was "
            "priced, and the remediation steps including CLI and Terraform where known."
        ),
        input_schema={
            "type": "object",
            "properties": {"finding_id": {"type": "string"}},
            "required": ["finding_id"],
        },
    ),
    ToolSpec(
        name="finops_search_resources",
        source=SOURCE,
        description=(
            "Search the inventory this scan collected. Returns type, region, state, "
            "monthly cost, and the ARN needed by finops_get_resource."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Free text over id and name."},
                "service": {"type": "string"},
                "region": {"type": "string"},
                "resource_type": {"type": "string", "description": "e.g. ec2:instance."},
                "state": {"type": "string", "description": "e.g. running, available."},
                "limit": {"type": "integer", "description": f"Default 20, max {MAX_ROWS}."},
            },
        },
    ),
    ToolSpec(
        name="finops_get_resource",
        source=SOURCE,
        description=(
            "One resource in full: configuration attributes, CloudWatch utilization "
            "metrics, tags, and cost. Use this before claiming a resource is idle."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "arn": {"type": "string", "description": "Full ARN, or a resource id or name."}
            },
            "required": ["arn"],
        },
    ),
]


class ScanTools:
    """Serves the tools above from one stored scan."""

    def __init__(self, store: ScanStore, scan_id: str) -> None:
        self._store = store
        self._scan_id = scan_id

    @property
    def tools(self) -> list[ToolSpec]:
        return list(TOOLS)

    def handles(self, name: str) -> bool:
        return any(tool.name == name for tool in TOOLS)

    def call(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        handlers = {
            "finops_cost_breakdown": self._cost_breakdown,
            "finops_search_findings": self._search_findings,
            "finops_get_finding": self._get_finding,
            "finops_search_resources": self._search_resources,
            "finops_get_resource": self._get_resource,
        }
        handler = handlers.get(name)
        if handler is None:
            return f"No such tool: {name}", True
        try:
            return _dump(handler(arguments)), False
        except Exception as exc:  # noqa: BLE001 - a bad argument should not end the turn
            return f"{name} failed: {exc}", True

    def _cost_breakdown(self, args: dict) -> dict:
        report = self._store.get_tco(self._scan_id)
        if report is None:
            return {"error": "This scan has no cost report."}

        dimension = args.get("dimension") or "all"
        breakdowns = {
            "service": report.by_service,
            "region": report.by_region,
            "usage_type": report.by_usage_type,
            "category": report.by_category,
            "effort": report.by_effort,
        }
        wanted = breakdowns if dimension == "all" else {dimension: breakdowns.get(dimension, [])}
        return {
            "currency": report.currency,
            "period": f"{report.period_start} to {report.period_end}",
            "monthly_run_rate": report.monthly_run_rate,
            "optimized_monthly_run_rate": report.optimized_monthly_run_rate,
            "identified_monthly_savings": report.identified_monthly_savings,
            "savings_percent": report.savings_percent,
            "forecast_next_month": report.forecast_next_month,
            "untagged_monthly_cost": report.untagged_monthly_cost,
            "commitment_coverage_percent": report.commitment_coverage_percent,
            **{
                f"by_{key}": [
                    {
                        "key": item.key,
                        "monthly_cost": round_money(item.amount),
                        "share_percent": item.share,
                        "monthly_savings": round_money(item.savings),
                    }
                    for item in items[:15]
                ]
                for key, items in wanted.items()
            },
        }

    def _search_findings(self, args: dict) -> dict:
        findings, total = self._store.query_findings(
            self._scan_id,
            search=args.get("query"),
            service=args.get("service"),
            category=args.get("category"),
            region=args.get("region"),
            min_savings=args.get("min_savings"),
            limit=_limit(args),
        )
        return {
            "total_matching": total,
            "returned": len(findings),
            "findings": [
                {
                    "id": finding.id,
                    "title": finding.title,
                    "category": finding.category,
                    "service": finding.service,
                    "region": finding.region,
                    "resource_id": finding.resource_id,
                    "monthly_savings": finding.estimated_monthly_savings,
                    "effort": finding.implementation_effort,
                    "risk": finding.risk,
                    "confidence": finding.confidence,
                    "source": finding.source,
                }
                for finding in findings
            ],
        }

    def _get_finding(self, args: dict) -> dict:
        finding_id = args.get("finding_id", "")
        findings, _ = self._store.query_findings(self._scan_id, limit=10_000)
        match = next((f for f in findings if f.id == finding_id), None)
        if match is None:
            return {"error": f"No finding {finding_id} in this scan."}
        return match.model_dump(mode="json")

    def _search_resources(self, args: dict) -> dict:
        resources, total = self._store.query_resources(
            self._scan_id,
            search=args.get("query"),
            service=args.get("service"),
            region=args.get("region"),
            resource_type=args.get("resource_type"),
            state=args.get("state"),
            limit=_limit(args),
        )
        return {
            "total_matching": total,
            "returned": len(resources),
            "resources": [
                {
                    "arn": resource.arn,
                    "resource_id": resource.resource_id,
                    "name": resource.name,
                    "type": resource.resource_type,
                    "service": resource.service,
                    "region": resource.region,
                    "state": resource.state,
                    "monthly_cost": resource.monthly_cost,
                    "cost_basis": resource.cost_basis,
                }
                for resource in resources
            ],
        }

    def _get_resource(self, args: dict) -> dict:
        wanted = (args.get("arn") or "").strip()
        # The store indexes id and name, not the ARN, and a model often has only the id.
        # Try the cheap search first, then fall back to a scan for an exact ARN match.
        candidates, _ = self._store.query_resources(self._scan_id, search=wanted, limit=MAX_ROWS)
        match = next(
            (r for r in candidates if wanted in (r.arn, r.resource_id, r.name)),
            None,
        )
        if match is None and wanted.startswith("arn:"):
            everything, _ = self._store.query_resources(self._scan_id, limit=10_000)
            match = next((r for r in everything if r.arn == wanted), None)
        if match is None:
            return {"error": f"No resource matching '{wanted}' in this scan."}
        return match.model_dump(mode="json")


def _limit(args: dict) -> int:
    try:
        requested = int(args.get("limit") or 20)
    except (TypeError, ValueError):
        requested = 20
    return max(1, min(requested, MAX_ROWS))


def _dump(payload: dict) -> str:
    return json.dumps(payload, indent=2, default=str)
