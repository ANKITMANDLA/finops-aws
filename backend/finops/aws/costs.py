"""Cost Explorer access.

``GetCostAndUsage`` with the ``AmortizedCost`` metric is the authoritative source for
what the account actually spends: it spreads upfront Reserved Instance and Savings Plan
payments across the periods they cover, so the run rate is not distorted by the month a
commitment was purchased.

Resource-level attribution (``GetCostAndUsageWithResources``) is attempted too, but it
is opt-in and only retains 14 days, so callers must treat it as a bonus rather than a
guarantee and fall back to list-price estimates.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from finops.aws.errors import NoteCollector, graceful
from finops.aws.session import AwsContext
from finops.model import CostRecord
from finops.util import safe_div

logger = logging.getLogger(__name__)

DAYS_PER_MONTH = 30.44
DEFAULT_METRIC = "AmortizedCost"

# GetCostAndUsageWithResources only serves the trailing 14 days.
RESOURCE_LEVEL_MAX_DAYS = 14

# Credits, refunds, and tax distort the picture of what infrastructure costs to run.
_USAGE_ONLY_FILTER: dict[str, Any] = {
    "Not": {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Credit", "Refund", "Tax"]}}
}


@dataclass
class CommitmentSummary:
    """Savings Plans and Reserved Instance posture."""

    savings_plans_coverage_percent: float | None = None
    savings_plans_utilization_percent: float | None = None
    reservation_coverage_percent: float | None = None
    reservation_utilization_percent: float | None = None
    savings_plans_recommendation: dict[str, Any] | None = None
    reservation_recommendations: list[dict[str, Any]] = field(default_factory=list)

    @property
    def blended_coverage_percent(self) -> float | None:
        values = [
            v
            for v in (self.savings_plans_coverage_percent, self.reservation_coverage_percent)
            if v is not None
        ]
        if not values:
            return None
        # Coverage is reported per commitment type against different denominators; the
        # larger of the two is the closest honest summary of "how much is committed".
        return max(values)


@dataclass
class CostSnapshot:
    """Everything the cost layer learned about the account in one pass."""

    period_start: date
    period_end: date
    metric: str = DEFAULT_METRIC
    records: list[CostRecord] = field(default_factory=list)
    total_cost: float = 0.0
    month_to_date_cost: float = 0.0
    previous_period_cost: float | None = None
    forecast_next_month: float | None = None
    forecast_lower: float | None = None
    forecast_upper: float | None = None
    service_totals: dict[str, float] = field(default_factory=dict)
    region_totals: dict[str, float] = field(default_factory=dict)
    usage_type_totals: dict[str, float] = field(default_factory=dict)
    daily_totals: dict[str, float] = field(default_factory=dict)
    resource_costs: dict[str, float] = field(default_factory=dict)
    resource_level_available: bool = False
    commitments: CommitmentSummary = field(default_factory=CommitmentSummary)

    @property
    def days_in_period(self) -> int:
        return max((self.period_end - self.period_start).days, 1)

    @property
    def daily_run_rate(self) -> float:
        return safe_div(self.total_cost, self.days_in_period)

    @property
    def monthly_run_rate(self) -> float:
        return self.daily_run_rate * DAYS_PER_MONTH

    def monthly_cost_for_resource(self, *keys: str | None) -> float | None:
        """Look up billed monthly cost by any identifier the resource is known under."""
        for key in keys:
            if key and key in self.resource_costs:
                return self.resource_costs[key]
        return None


class CostExplorer:
    """Thin, failure-tolerant wrapper over the Cost Explorer API."""

    def __init__(self, aws: AwsContext, notes: NoteCollector | None = None) -> None:
        self.aws = aws
        self.notes = notes or NoteCollector()

    @property
    def client(self):
        return self.aws.client("ce")

    # ------------------------------------------------------------------- core

    def _cost_and_usage(
        self,
        start: date,
        end: date,
        *,
        granularity: str = "DAILY",
        group_by: Sequence[dict[str, str]] | None = None,
        metric: str = DEFAULT_METRIC,
        cost_filter: dict[str, Any] | None = _USAGE_ONLY_FILTER,
    ) -> list[CostRecord]:
        """Run a paginated GetCostAndUsage and flatten it into CostRecords."""
        kwargs: dict[str, Any] = {
            "TimePeriod": {"Start": start.isoformat(), "End": end.isoformat()},
            "Granularity": granularity,
            "Metrics": [metric],
        }
        if group_by:
            kwargs["GroupBy"] = list(group_by)
        if cost_filter:
            kwargs["Filter"] = cost_filter

        records: list[CostRecord] = []
        next_token: str | None = None
        while True:
            if next_token:
                kwargs["NextPageToken"] = next_token
            response = self.client.get_cost_and_usage(**kwargs)
            records.extend(
                _flatten_results(
                    response.get("ResultsByTime", []),
                    granularity=granularity,
                    metric=metric,
                    group_by=group_by,
                )
            )
            next_token = response.get("NextPageToken")
            if not next_token:
                break
        return records

    # -------------------------------------------------------------- collection

    def snapshot(self, lookback_days: int) -> CostSnapshot:
        """Gather the full cost picture. Each section degrades independently."""
        end = date.today()
        start = end - timedelta(days=lookback_days)
        snapshot = CostSnapshot(period_start=start, period_end=end)

        with graceful(self.notes, "ce:GetCostAndUsage"):
            daily_by_service = self._cost_and_usage(
                start, end, group_by=[{"Type": "DIMENSION", "Key": "SERVICE"}]
            )
            snapshot.records.extend(daily_by_service)
            snapshot.total_cost = sum(r.amount for r in daily_by_service)
            for record in daily_by_service:
                service = record.dimensions.get("SERVICE", "Unknown")
                snapshot.service_totals[service] = (
                    snapshot.service_totals.get(service, 0.0) + record.amount
                )
                day = record.period_start.isoformat()
                snapshot.daily_totals[day] = snapshot.daily_totals.get(day, 0.0) + record.amount

        with graceful(self.notes, "ce:GetCostAndUsage(region)"):
            by_region = self._cost_and_usage(
                start,
                end,
                granularity="MONTHLY" if lookback_days > 62 else "DAILY",
                group_by=[{"Type": "DIMENSION", "Key": "REGION"}],
            )
            snapshot.records.extend(by_region)
            for record in by_region:
                region = record.dimensions.get("REGION", "global")
                snapshot.region_totals[region] = (
                    snapshot.region_totals.get(region, 0.0) + record.amount
                )

        with graceful(self.notes, "ce:GetCostAndUsage(usage type)"):
            by_usage_type = self._cost_and_usage(
                start,
                end,
                granularity="MONTHLY",
                group_by=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
            )
            snapshot.records.extend(by_usage_type)
            for record in by_usage_type:
                usage_type = record.dimensions.get("USAGE_TYPE", "Unknown")
                snapshot.usage_type_totals[usage_type] = (
                    snapshot.usage_type_totals.get(usage_type, 0.0) + record.amount
                )

        snapshot.month_to_date_cost = self._month_to_date(end)
        snapshot.previous_period_cost = self._previous_period(start, lookback_days)
        self._add_forecast(snapshot)
        self._add_resource_costs(snapshot, end)
        snapshot.commitments = self.commitments(start, end)
        return snapshot

    def _month_to_date(self, today: date) -> float:
        start = today.replace(day=1)
        if start >= today:
            return 0.0
        total = 0.0
        with graceful(self.notes, "ce:GetCostAndUsage(month to date)"):
            records = self._cost_and_usage(start, today, granularity="MONTHLY")
            total = sum(r.amount for r in records)
        return total

    def _previous_period(self, start: date, lookback_days: int) -> float | None:
        """Same-length window immediately before the analysis period, for a delta."""
        previous_start = start - timedelta(days=lookback_days)
        total: float | None = None
        with graceful(self.notes, "ce:GetCostAndUsage(previous period)"):
            records = self._cost_and_usage(previous_start, start, granularity="MONTHLY")
            total = sum(r.amount for r in records)
        return total

    def _add_forecast(self, snapshot: CostSnapshot) -> None:
        # The forecast window must start in the future.
        start = date.today() + timedelta(days=1)
        end = start + timedelta(days=30)
        with graceful(self.notes, "ce:GetCostForecast"):
            response = self.client.get_cost_forecast(
                TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
                Metric="AMORTIZED_COST",
                Granularity="DAILY",
                PredictionIntervalLevel=80,
            )
            snapshot.forecast_next_month = float(response.get("Total", {}).get("Amount", 0.0))
            lower = upper = 0.0
            for entry in response.get("ForecastResultsByTime", []):
                lower += float(entry.get("PredictionIntervalLowerBound") or 0.0)
                upper += float(entry.get("PredictionIntervalUpperBound") or 0.0)
            snapshot.forecast_lower = lower or None
            snapshot.forecast_upper = upper or None

    def _add_resource_costs(self, snapshot: CostSnapshot, end: date) -> None:
        """Best-effort per-resource billed cost for the services that spend the most."""
        if not snapshot.service_totals:
            return
        start = end - timedelta(days=RESOURCE_LEVEL_MAX_DAYS)
        top_services = [
            service
            for service, _ in sorted(
                snapshot.service_totals.items(), key=lambda kv: kv[1], reverse=True
            )[:10]
        ]
        window_days = max((end - start).days, 1)

        with graceful(self.notes, "ce:GetCostAndUsageWithResources"):
            totals: dict[str, float] = {}
            next_token: str | None = None
            while True:
                kwargs: dict[str, Any] = {
                    "TimePeriod": {"Start": start.isoformat(), "End": end.isoformat()},
                    "Granularity": "DAILY",
                    "Metrics": [DEFAULT_METRIC],
                    "GroupBy": [{"Type": "DIMENSION", "Key": "RESOURCE_ID"}],
                    # A filter is mandatory for this operation.
                    "Filter": {"Dimensions": {"Key": "SERVICE", "Values": top_services}},
                }
                if next_token:
                    kwargs["NextPageToken"] = next_token
                response = self.client.get_cost_and_usage_with_resources(**kwargs)
                for period in response.get("ResultsByTime", []):
                    for group in period.get("Groups", []):
                        resource_id = group.get("Keys", ["NoResourceId"])[0]
                        if resource_id in ("NoResourceId", ""):
                            continue
                        amount = float(
                            group.get("Metrics", {}).get(DEFAULT_METRIC, {}).get("Amount", 0.0)
                        )
                        totals[resource_id] = totals.get(resource_id, 0.0) + amount
                next_token = response.get("NextPageToken")
                if not next_token:
                    break

            # Normalize the 14-day window to a monthly figure.
            scale = DAYS_PER_MONTH / window_days
            snapshot.resource_costs = {
                resource_id: round(amount * scale, 4) for resource_id, amount in totals.items()
            }
            snapshot.resource_level_available = bool(snapshot.resource_costs)

        if not snapshot.resource_level_available and not self.notes.has_problem(
            "ce:GetCostAndUsageWithResources"
        ):
            self.notes.add(
                "ce:GetCostAndUsageWithResources",
                "unavailable",
                "Resource-level cost data returned nothing. Per-resource costs fall back to "
                "list-price estimates.",
                remedy="Enable resource-level data in Cost Explorer preferences (daily "
                "granularity, per service). It becomes available about 48 hours later.",
            )

    # ------------------------------------------------------------- commitments

    def commitments(self, start: date, end: date) -> CommitmentSummary:
        summary = CommitmentSummary()
        period = {"Start": start.isoformat(), "End": end.isoformat()}

        with graceful(self.notes, "ce:GetSavingsPlansCoverage"):
            response = self.client.get_savings_plans_coverage(
                TimePeriod=period, Granularity="MONTHLY"
            )
            percentages = [
                float(entry.get("Coverage", {}).get("CoveragePercentage", 0.0))
                for entry in response.get("SavingsPlansCoverages", [])
            ]
            if percentages:
                summary.savings_plans_coverage_percent = round(
                    sum(percentages) / len(percentages), 2
                )

        with graceful(self.notes, "ce:GetSavingsPlansUtilization"):
            response = self.client.get_savings_plans_utilization(TimePeriod=period)
            utilization = response.get("Total", {}).get("Utilization", {})
            if utilization:
                summary.savings_plans_utilization_percent = round(
                    float(utilization.get("UtilizationPercentage", 0.0)), 2
                )

        with graceful(self.notes, "ce:GetReservationCoverage"):
            response = self.client.get_reservation_coverage(
                TimePeriod=period, Granularity="MONTHLY"
            )
            coverage = response.get("Total", {}).get("CoverageHours", {})
            if coverage:
                summary.reservation_coverage_percent = round(
                    float(coverage.get("CoverageHoursPercentage", 0.0)), 2
                )

        with graceful(self.notes, "ce:GetReservationUtilization"):
            response = self.client.get_reservation_utilization(TimePeriod=period)
            total = response.get("Total", {})
            if total:
                summary.reservation_utilization_percent = round(
                    float(total.get("UtilizationPercentage", 0.0)), 2
                )

        with graceful(self.notes, "ce:GetSavingsPlansPurchaseRecommendation"):
            response = self.client.get_savings_plans_purchase_recommendation(
                SavingsPlansType="COMPUTE_SP",
                TermInYears="ONE_YEAR",
                PaymentOption="NO_UPFRONT",
                LookbackPeriodInDays="THIRTY_DAYS",
            )
            detail = response.get("SavingsPlansPurchaseRecommendation", {}).get(
                "SavingsPlansPurchaseRecommendationSummary", {}
            )
            if detail:
                summary.savings_plans_recommendation = {
                    "estimated_monthly_savings": float(
                        detail.get("EstimatedMonthlySavingsAmount") or 0.0
                    ),
                    "estimated_savings_percentage": float(
                        detail.get("EstimatedSavingsPercentage") or 0.0
                    ),
                    "hourly_commitment": float(detail.get("HourlyCommitmentToPurchase") or 0.0),
                    "current_on_demand_spend": float(detail.get("CurrentOnDemandSpend") or 0.0),
                    "estimated_roi": float(detail.get("EstimatedROI") or 0.0),
                    "term": "1 year, no upfront, Compute Savings Plan",
                }

        for service in (
            "Amazon Elastic Compute Cloud - Compute",
            "Amazon Relational Database Service",
            "Amazon ElastiCache",
            "Amazon OpenSearch Service",
        ):
            with graceful(self.notes, "ce:GetReservationPurchaseRecommendation"):
                response = self.client.get_reservation_purchase_recommendation(
                    Service=service,
                    LookbackPeriodInDays="THIRTY_DAYS",
                    TermInYears="ONE_YEAR",
                    PaymentOption="NO_UPFRONT",
                )
                for recommendation in response.get("Recommendations", []):
                    detail = recommendation.get("RecommendationSummary", {})
                    savings = float(detail.get("TotalEstimatedMonthlySavingsAmount") or 0.0)
                    if savings <= 0:
                        continue
                    summary.reservation_recommendations.append(
                        {
                            "service": service,
                            "estimated_monthly_savings": savings,
                            "currency": detail.get("CurrencyCode", "USD"),
                            "term": "1 year, no upfront, Reserved Instances",
                        }
                    )
        return summary


def _flatten_results(
    results: list[dict[str, Any]],
    *,
    granularity: str,
    metric: str,
    group_by: Sequence[dict[str, str]] | None,
) -> list[CostRecord]:
    """Turn Cost Explorer's nested response into flat CostRecords."""
    group_keys = [entry["Key"] for entry in group_by] if group_by else []
    records: list[CostRecord] = []

    for period in results:
        start = date.fromisoformat(period["TimePeriod"]["Start"])
        end = date.fromisoformat(period["TimePeriod"]["End"])
        groups = period.get("Groups") or []

        if not groups:
            amount = float(period.get("Total", {}).get(metric, {}).get("Amount", 0.0))
            if amount:
                records.append(
                    CostRecord(
                        period_start=start,
                        period_end=end,
                        granularity=granularity,  # type: ignore[arg-type]
                        metric=metric,
                        amount=amount,
                        unit=period.get("Total", {}).get(metric, {}).get("Unit", "USD"),
                    )
                )
            continue

        for group in groups:
            values = group.get("Keys", [])
            amount = float(group.get("Metrics", {}).get(metric, {}).get("Amount", 0.0))
            if not amount:
                continue
            records.append(
                CostRecord(
                    period_start=start,
                    period_end=end,
                    granularity=granularity,  # type: ignore[arg-type]
                    metric=metric,
                    amount=amount,
                    unit=group.get("Metrics", {}).get(metric, {}).get("Unit", "USD"),
                    dimensions=dict(zip(group_keys, values, strict=False)),
                )
            )
    return records
