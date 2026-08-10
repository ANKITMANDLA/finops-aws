from __future__ import annotations

from datetime import date

import pytest
from tests.factories import make_finding, make_resource

from finops.aws.costs import CommitmentSummary, CostSnapshot
from finops.model import TcoReport
from finops.tco import build_tco_report, compare_scans, rank_findings, summarize_for_advisor


def snapshot(**kwargs) -> CostSnapshot:
    defaults = dict(
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        total_cost=3000.0,
        month_to_date_cost=1500.0,
        previous_period_cost=2500.0,
        forecast_next_month=3200.0,
        service_totals={"Amazon EC2": 2000.0, "Amazon S3": 700.0, "Amazon RDS": 300.0},
        region_totals={"us-east-1": 2400.0, "eu-west-1": 600.0},
        daily_totals={"2026-07-01": 100.0, "2026-07-02": 110.0},
    )
    defaults.update(kwargs)
    return CostSnapshot(**defaults)


def test_headline_total_comes_from_cost_explorer_not_from_estimates():
    # Per-resource estimates deliberately disagree with the bill.
    resources = [make_resource("i-1", monthly_cost=50.0)]
    findings = [make_finding(savings=100.0, resource_arn="arn:1")]

    report = build_tco_report(snapshot(), findings, resources)

    assert report.total_cost == 3000.0
    # 3000 over 30 days scaled to a 30.44 day month.
    assert report.monthly_run_rate == pytest.approx(3044.0, abs=0.5)
    assert report.daily_run_rate == pytest.approx(100.0)


def test_optimized_run_rate_is_the_bill_minus_identified_savings():
    findings = [
        make_finding("a", savings=200.0, resource_arn="arn:1"),
        make_finding("b", savings=100.0, resource_arn="arn:2"),
    ]

    report = build_tco_report(snapshot(), findings, [])

    assert report.identified_monthly_savings == 300.0
    assert report.optimized_monthly_run_rate == pytest.approx(
        report.monthly_run_rate - 300.0, abs=0.01
    )
    assert report.savings_percent == pytest.approx(9.86, abs=0.05)


def test_savings_cannot_exceed_the_actual_bill():
    # A rule with a runaway estimate must not produce a negative optimized run rate.
    findings = [make_finding(savings=999_999.0, resource_arn="arn:1")]

    report = build_tco_report(snapshot(), findings, [])

    assert report.identified_monthly_savings == report.monthly_run_rate
    assert report.optimized_monthly_run_rate == 0.0


def test_service_breakdown_carries_shares_and_attributed_savings():
    findings = [
        make_finding("a", savings=120.0, service="Amazon EC2", resource_arn="arn:1"),
        make_finding("b", savings=30.0, service="Amazon S3", resource_arn="arn:2"),
    ]

    report = build_tco_report(snapshot(), findings, [])

    ec2 = next(item for item in report.by_service if item.key == "Amazon EC2")
    assert ec2.amount == 2000.0
    assert ec2.share == pytest.approx(66.67, abs=0.01)
    assert ec2.savings == 120.0
    assert [item.key for item in report.by_service] == ["Amazon EC2", "Amazon S3", "Amazon RDS"]


def test_savings_are_matched_to_cost_explorer_service_names():
    # Collectors say "EC2" and "EBS"; the bill says something much longer.
    findings = [
        make_finding("a", savings=100.0, service="EC2", resource_arn="arn:1"),
        make_finding("b", savings=40.0, service="EBS", resource_arn="arn:2"),
        make_finding("c", savings=10.0, service="Nonexistent", resource_arn="arn:3"),
    ]
    report = build_tco_report(
        snapshot(
            service_totals={
                "Amazon Elastic Compute Cloud - Compute": 2000.0,
                "EC2 - Other": 700.0,
                "Amazon Simple Storage Service": 300.0,
            }
        ),
        findings,
        [],
    )

    by_key = {item.key: item.savings for item in report.by_service}
    assert by_key["Amazon Elastic Compute Cloud - Compute"] == 100.0
    # EBS bills under EC2 - Other, so that is where its savings belong.
    assert by_key["EC2 - Other"] == 40.0
    # A service with no matching cost row is simply not attributed anywhere.
    assert sum(by_key.values()) == 140.0


def test_long_breakdowns_fold_their_tail_into_one_row():
    services = {f"Service {index}": float(100 - index) for index in range(30)}
    report = build_tco_report(
        snapshot(service_totals=services, total_cost=sum(services.values())), [], []
    )

    assert len(report.by_service) == 13  # 12 named plus the grouped tail
    assert report.by_service[-1].key.startswith("Other (18 more)")
    assert sum(item.amount for item in report.by_service) == pytest.approx(
        sum(services.values()), abs=0.05
    )


def test_period_over_period_change_is_computed():
    report = build_tco_report(snapshot(), [], [])
    # 3000 against a previous 2500.
    assert report.change_percent == 20.0

    without_history = build_tco_report(snapshot(previous_period_cost=None), [], [])
    assert without_history.change_percent is None


def test_savings_are_grouped_by_category_and_effort():
    findings = [
        make_finding("a", savings=100.0, category="idle", resource_arn="arn:1"),
        make_finding("b", savings=50.0, category="storage", resource_arn="arn:2"),
        make_finding("c", savings=25.0, category="idle", resource_arn="arn:3"),
    ]
    findings[1].implementation_effort = "high"

    report = build_tco_report(snapshot(), findings, [])

    categories = {item.key: item.amount for item in report.by_category}
    assert categories == {"Idle resources": 125.0, "Storage optimization": 50.0}
    efforts = {item.key: item.amount for item in report.by_effort}
    assert efforts == {"Low effort": 125.0, "High effort": 50.0}


def test_untagged_cost_counts_only_resources_without_an_owner():
    resources = [
        make_resource("i-1", tags={"Owner": "platform"}, monthly_cost=500.0),
        make_resource("i-2", tags={}, monthly_cost=300.0),
        make_resource("i-3", tags={"Name": "just-a-label"}, monthly_cost=200.0),
    ]

    report = build_tco_report(snapshot(), [], resources)

    # A Name tag says what it is, not who owns it.
    assert report.untagged_monthly_cost == 500.0


def test_commitment_coverage_is_carried_through():
    report = build_tco_report(
        snapshot(
            commitments=CommitmentSummary(
                savings_plans_coverage_percent=45.0, reservation_coverage_percent=12.0
            )
        ),
        [],
        [],
    )
    assert report.commitment_coverage_percent == 45.0


def test_daily_trend_is_ordered_by_date():
    report = build_tco_report(
        snapshot(daily_totals={"2026-07-03": 90.0, "2026-07-01": 100.0, "2026-07-02": 110.0}),
        [],
        [],
    )
    assert [item.key for item in report.daily_trend] == [
        "2026-07-01",
        "2026-07-02",
        "2026-07-03",
    ]


def test_ranking_prefers_easy_wins_over_big_risky_projects():
    easy = make_finding("gp2_to_gp3", savings=100.0, resource_arn="arn:easy")
    easy.implementation_effort, easy.risk, easy.confidence = "low", "low", "high"

    hard = make_finding("graviton", savings=200.0, resource_arn="arn:hard")
    hard.implementation_effort, hard.risk, hard.confidence = "high", "high", "low"

    ranked = rank_findings([hard, easy])

    # 200 discounted for high effort, high risk, and low confidence lands under 100.
    assert [f.rule_id for f in ranked] == ["gp2_to_gp3", "graviton"]
    assert easy.priority_score == 100.0
    assert hard.priority_score == pytest.approx(200 * 0.4 * 0.6 * 0.6, abs=0.01)


def test_advisor_summary_stays_compact_and_hides_raw_inventory():
    resources = [make_resource(f"i-{index}", monthly_cost=10.0) for index in range(200)]
    findings = [
        make_finding(f"r{index}", savings=float(index), resource_arn=f"arn:{index}")
        for index in range(100)
    ]
    report = build_tco_report(snapshot(), findings, resources)

    summary = summarize_for_advisor(report, findings, resources, max_findings=10)

    assert len(summary["top_findings"]) == 10
    # Aggregated by type, never resource by resource.
    assert summary["inventory"] == [
        {"resource_type": "ec2:instance", "count": 200, "monthly_cost": 2000.0}
    ]
    assert summary["monthly_run_rate"] == report.monthly_run_rate
    assert "us-east-1" in summary["regions_in_use"]
    assert all("arn" not in str(entry) for entry in summary["inventory"])


def test_scan_comparison_handles_a_first_ever_scan():
    current = TcoReport(
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        monthly_run_rate=1000.0,
        identified_monthly_savings=200.0,
    )
    previous = TcoReport(
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 30),
        monthly_run_rate=800.0,
        identified_monthly_savings=150.0,
    )

    assert compare_scans(current, None)["run_rate_change"] is None

    delta = compare_scans(current, previous)
    assert delta["run_rate_change"] == 200.0
    assert delta["run_rate_change_percent"] == 25.0
    assert delta["savings_change"] == 50.0
