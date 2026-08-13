"""End-to-end scan orchestration.

One pass over the account: inventory, cost, metrics, AWS-native recommendations, our
own rules, the TCO report, and finally the advisor. Every stage degrades on its own -
a denied API costs you that stage's data, not the scan.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from finops.agent.advisor import Advisor, build_advisor
from finops.attribution import attribute_costs
from finops.aws.collectors import CollectionContext, collect_inventory
from finops.aws.costs import CostExplorer, CostSnapshot
from finops.aws.errors import NoteCollector, graceful
from finops.aws.metrics import MetricsCollector
from finops.aws.native_recs import NativeRecommendations
from finops.aws.pricing import build_pricing
from finops.aws.session import AwsContext
from finops.config import Settings, get_settings
from finops.model import Finding, Scan, utcnow
from finops.rules import RuleContext, merge_findings, run_rules
from finops.store import ScanStore
from finops.tco import build_tco_report, rank_findings

logger = logging.getLogger(__name__)

# Stage names, in the order they run. The UI shows these as scan progress.
STAGES = ("inventory", "costs", "metrics", "pricing", "native", "rules", "tco", "advice")

ProgressCallback = Callable[[str, str], None]


@dataclass
class ScanOptions:
    """Knobs for one scan. Defaults run everything."""

    regions: list[str] | None = None
    collectors: list[str] | None = None
    skip_collectors: list[str] = field(default_factory=list)
    rules: list[str] | None = None
    skip_rules: list[str] = field(default_factory=list)
    with_metrics: bool = True
    with_native: bool = True
    with_advice: bool = True
    persist: bool = True


def run_scan(
    settings: Settings | None = None,
    options: ScanOptions | None = None,
    *,
    aws: AwsContext | None = None,
    store: ScanStore | None = None,
    advisor: Advisor | None = None,
    progress: ProgressCallback | None = None,
) -> Scan:
    """Execute a full read-only scan and return the assembled result."""
    settings = settings or get_settings()
    options = options or ScanOptions()
    aws = aws or AwsContext(settings=settings)
    notes = NoteCollector()
    started = time.monotonic()

    def step(stage: str, message: str) -> None:
        logger.info("[%s] %s", stage, message)
        if progress:
            progress(stage, message)

    # Without this, a missing profile produces a "successful" scan of an empty account:
    # every collector reports AccessDenied and the report reads as zero spend, zero waste.
    aws.verify_credentials()

    regions = list(options.regions) if options.regions else aws.regions
    scan = Scan(
        scan_id=_new_scan_id(),
        account_id=aws.account_id,
        account_alias=aws.account_alias,
        started_at=utcnow(),
        regions=regions,
    )
    step("inventory", f"Scanning {len(regions)} region(s) as account {scan.account_id}")

    # --- inventory ---
    ctx = CollectionContext(aws=aws, notes=notes, target_regions=regions)
    resources = collect_inventory(
        ctx,
        only=options.collectors,
        skip=options.skip_collectors,
        regions=regions,
        progress=lambda key, region, count: (
            progress(f"inventory:{key}", f"{region}: {count} resource(s)") if progress else None
        ),
    )
    step("inventory", f"Found {len(resources)} resources")

    # --- cost ---
    step("costs", "Querying Cost Explorer")
    explorer = CostExplorer(aws, notes)
    snapshot = explorer.snapshot(settings.cost_lookback_days)
    step(
        "costs",
        f"${snapshot.total_cost:,.2f} over {snapshot.days_in_period} days "
        f"({'resource-level' if snapshot.resource_level_available else 'service-level'} detail)",
    )

    # --- utilization ---
    if options.with_metrics and resources:
        step("metrics", f"Collecting CloudWatch metrics over {settings.metric_lookback_days} days")
        MetricsCollector(aws, notes).collect(resources)

    # --- per-resource cost ---
    step("pricing", "Attributing cost to resources")
    pricing = build_pricing(aws, notes)
    attribute_costs(resources, snapshot, pricing)

    # --- findings ---
    findings: list[Finding] = []
    if options.with_native:
        step("native", "Reading AWS optimization recommendations")
        with graceful(notes, "native-recommendations"):
            findings.extend(NativeRecommendations(aws, notes).collect(regions))

    step("rules", f"Evaluating rules against {len(resources)} resources")
    rule_ctx = RuleContext(
        resources=resources,
        cost=snapshot,
        pricing=pricing,
        thresholds=settings.thresholds,
    )
    findings.extend(run_rules(rule_ctx, options.rules, options.skip_rules))
    findings = rank_findings(
        merge_findings(findings, min_savings=settings.thresholds.min_monthly_savings_usd)
    )
    step("rules", f"{len(findings)} finding(s) after de-duplication")

    # --- report ---
    step("tco", "Building the TCO report")
    scan.resources = resources
    scan.costs = snapshot.records
    scan.findings = findings
    scan.tco = build_tco_report(snapshot, findings, resources)
    scan.notes = notes.notes

    # --- narrative ---
    if options.with_advice:
        step("advice", "Generating architectural recommendations")
        advisor = advisor or build_advisor(settings, aws)
        scan.advice = advisor.advise(scan.tco, findings, resources, scan.notes)

    scan.finished_at = utcnow()
    scan.duration_seconds = round(time.monotonic() - started, 2)
    step(
        "done",
        f"Run rate ${scan.tco.monthly_run_rate:,.2f}/mo, identified savings "
        f"${scan.tco.identified_monthly_savings:,.2f}/mo in {scan.duration_seconds:.1f}s",
    )

    if options.persist:
        (store or ScanStore(settings.db_path)).save_scan(scan)
    return scan


def regenerate_advice(
    scan: Scan,
    settings: Settings | None = None,
    *,
    aws: AwsContext | None = None,
    store: ScanStore | None = None,
    advisor: Advisor | None = None,
) -> Scan:
    """Re-run only the LLM layer against a stored scan.

    Useful for switching providers or re-prompting without paying for another Cost
    Explorer pass.
    """
    settings = settings or get_settings()
    if scan.tco is None:
        raise ValueError(f"Scan {scan.scan_id} has no TCO report to advise on")
    advisor = advisor or build_advisor(settings, aws or AwsContext(settings=settings))
    scan.advice = advisor.advise(scan.tco, scan.findings, scan.resources, scan.notes)
    (store or ScanStore(settings.db_path)).save_advice(scan.scan_id, scan.advice)
    return scan


def _new_scan_id(now: datetime | None = None) -> str:
    """Sortable, human-readable id: ``20260810T142201Z-3f2a``."""
    stamp = (now or utcnow()).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:4]}"


def snapshot_totals(snapshot: CostSnapshot) -> dict[str, float]:  # pragma: no cover - debugging
    return {
        "total": snapshot.total_cost,
        "monthly_run_rate": snapshot.monthly_run_rate,
        "services": len(snapshot.service_totals),
    }


def available_stages() -> Sequence[str]:
    return STAGES
