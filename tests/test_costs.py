from __future__ import annotations

from datetime import date

import pytest
from tests.fakes import FakeAwsContext, FakeCostExplorerClient

from finops.aws.costs import DAYS_PER_MONTH, CostExplorer, CostSnapshot, _flatten_results
from finops.aws.errors import NoteCollector


@pytest.fixture
def explorer(settings):
    client = FakeCostExplorerClient()
    notes = NoteCollector()
    return CostExplorer(aws=FakeAwsContext(client, settings), notes=notes), client, notes


def test_flatten_results_handles_grouped_and_ungrouped_periods():
    grouped = _flatten_results(
        [
            {
                "TimePeriod": {"Start": "2026-07-01", "End": "2026-07-02"},
                "Groups": [
                    {
                        "Keys": ["Amazon EC2"],
                        "Metrics": {"AmortizedCost": {"Amount": "12.5", "Unit": "USD"}},
                    },
                    # Zero-cost groups are noise and must be dropped.
                    {
                        "Keys": ["Amazon SNS"],
                        "Metrics": {"AmortizedCost": {"Amount": "0", "Unit": "USD"}},
                    },
                ],
            }
        ],
        granularity="DAILY",
        metric="AmortizedCost",
        group_by=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )
    assert len(grouped) == 1
    assert grouped[0].dimensions == {"SERVICE": "Amazon EC2"}
    assert grouped[0].amount == 12.5
    assert grouped[0].period_start == date(2026, 7, 1)

    ungrouped = _flatten_results(
        [
            {
                "TimePeriod": {"Start": "2026-07-01", "End": "2026-08-01"},
                "Total": {"AmortizedCost": {"Amount": "255.0", "Unit": "USD"}},
                "Groups": [],
            }
        ],
        granularity="MONTHLY",
        metric="AmortizedCost",
        group_by=None,
    )
    assert ungrouped[0].amount == 255.0
    assert ungrouped[0].dimensions == {}


def test_snapshot_aggregates_services_regions_and_usage_types(explorer):
    cost_explorer, _, notes = explorer
    snapshot = cost_explorer.snapshot(lookback_days=30)

    # Both pages of the SERVICE query are included.
    assert snapshot.service_totals == {
        "Amazon Elastic Compute Cloud - Compute": 210.0,
        "Amazon Simple Storage Service": 45.0,
    }
    assert snapshot.total_cost == 255.0
    assert snapshot.region_totals == {"us-east-1": 200.0, "eu-west-1": 55.0}
    assert snapshot.usage_type_totals["NatGateway-Hours"] == 32.85
    assert snapshot.daily_totals == {"2026-07-01": 120.0, "2026-07-02": 135.0}
    assert notes.notes == []


def test_snapshot_computes_run_rates_from_the_period_length(explorer):
    cost_explorer, _, _ = explorer
    snapshot = cost_explorer.snapshot(lookback_days=30)

    assert snapshot.days_in_period == 30
    assert snapshot.daily_run_rate == pytest.approx(255.0 / 30)
    assert snapshot.monthly_run_rate == pytest.approx(255.0 / 30 * DAYS_PER_MONTH)


def test_forecast_sums_the_prediction_interval(explorer):
    cost_explorer, _, _ = explorer
    snapshot = cost_explorer.snapshot(lookback_days=30)

    assert snapshot.forecast_next_month == 300.0
    assert snapshot.forecast_lower == pytest.approx(270.0)
    assert snapshot.forecast_upper == pytest.approx(330.0)


def test_resource_costs_are_scaled_to_a_month_and_skip_unattributed(explorer):
    cost_explorer, _, _ = explorer
    snapshot = cost_explorer.snapshot(lookback_days=30)

    assert snapshot.resource_level_available is True
    assert "NoResourceId" not in snapshot.resource_costs
    # $7 over the 14-day window scales up to a month.
    assert snapshot.resource_costs["i-0123456789abcdef0"] == pytest.approx(
        7.0 * DAYS_PER_MONTH / 14, rel=1e-3
    )


def test_missing_resource_level_data_produces_an_actionable_note(settings):
    client = FakeCostExplorerClient(resource_costs=False)
    notes = NoteCollector()
    snapshot = CostExplorer(FakeAwsContext(client, settings), notes).snapshot(lookback_days=30)

    assert snapshot.resource_level_available is False
    note = next(n for n in notes.notes if n.capability == "ce:GetCostAndUsageWithResources")
    assert note.status == "unavailable"
    assert "Cost Explorer preferences" in (note.remedy or "")


def test_commitment_posture_is_summarized(explorer):
    cost_explorer, _, _ = explorer
    commitments = cost_explorer.snapshot(lookback_days=30).commitments

    assert commitments.savings_plans_coverage_percent == 50.0
    assert commitments.savings_plans_utilization_percent == 97.5
    assert commitments.reservation_coverage_percent == 22.0
    assert commitments.reservation_utilization_percent == 88.0
    assert commitments.blended_coverage_percent == 50.0
    assert commitments.savings_plans_recommendation["estimated_monthly_savings"] == 120.5
    assert [r["service"] for r in commitments.reservation_recommendations] == [
        "Amazon Relational Database Service"
    ]


def test_a_denied_call_is_recorded_without_aborting_the_snapshot(settings):
    client = FakeCostExplorerClient(fail={"get_cost_forecast", "get_savings_plans_coverage"})
    notes = NoteCollector()
    snapshot = CostExplorer(FakeAwsContext(client, settings), notes).snapshot(lookback_days=30)

    # The parts that worked are still populated.
    assert snapshot.total_cost == 255.0
    assert snapshot.forecast_next_month is None
    assert snapshot.commitments.savings_plans_coverage_percent is None
    assert snapshot.commitments.savings_plans_utilization_percent == 97.5

    denied = {n.capability for n in notes.notes if n.status == "denied"}
    assert "ce:GetCostForecast" in denied
    assert "ce:GetSavingsPlansCoverage" in denied


def test_monthly_cost_lookup_tries_every_identifier():
    snapshot = CostSnapshot(
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        resource_costs={"vol-abc": 12.0},
    )
    assert snapshot.monthly_cost_for_resource("arn:aws:ec2:...:volume/vol-abc", "vol-abc") == 12.0
    assert snapshot.monthly_cost_for_resource(None, "vol-missing") is None
