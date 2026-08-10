from __future__ import annotations

import pytest

from finops.config import Settings
from finops.demo import _instance_hourly, demo_cost_snapshot, run_demo_scan
from finops.store import ScanStore


@pytest.fixture(scope="module")
def demo_run(tmp_path_factory):
    """One dry run shared by every assertion here; it seeds a whole mock account."""
    path = tmp_path_factory.mktemp("demo")
    settings = Settings(db_path=path / "finops.db", llm_provider="none")
    store = ScanStore(settings.db_path)
    scan = run_demo_scan(settings, store=store, with_advice=False)
    return scan, store


@pytest.fixture(scope="module")
def demo_scan(demo_run):
    return demo_run[0]


def test_dry_run_produces_a_usable_scan_without_credentials(demo_scan):
    assert demo_scan.tco is not None
    assert demo_scan.tco.monthly_run_rate > 0
    assert 0 < demo_scan.tco.identified_monthly_savings < demo_scan.tco.monthly_run_rate
    assert demo_scan.duration_seconds > 0


def test_the_estate_covers_the_services_the_rules_care_about(demo_scan):
    types = {resource.resource_type for resource in demo_scan.resources}
    for expected in (
        "ec2:instance",
        "ebs:volume",
        "ebs:snapshot",
        "ec2:elastic-ip",
        "ec2:nat-gateway",
        "rds:db-instance",
        "s3:bucket",
        "dynamodb:table",
        "logs:log-group",
    ):
        assert expected in types, f"missing {expected}"


def test_mock_provider_snapshots_are_not_reported_as_the_account_s_own(demo_scan):
    snapshots = [r for r in demo_scan.resources if r.resource_type == "ebs:snapshot"]
    # moto pre-populates thousands; only the three this demo created should survive.
    assert len(snapshots) == 3
    assert all(
        str(snapshot.attributes["description"]).startswith("pre-migration")
        for snapshot in snapshots
    )


def test_findings_span_several_categories(demo_scan):
    categories = {finding.category for finding in demo_scan.findings}
    assert {"idle", "storage", "rightsizing"} <= categories


def test_a_busy_instance_is_never_called_idle(demo_scan):
    web = next(r for r in demo_scan.resources if (r.name or "") == "web-1")
    against_web = [f for f in demo_scan.findings if f.resource_id == web.resource_id]

    # A Graviton migration is fair game for a busy host; "idle" or "downsize" is not.
    assert all(finding.category != "idle" for finding in against_web)
    assert not any(finding.rule_id.endswith("underutilized") for finding in against_web)


def test_an_idle_instance_is_flagged(demo_scan):
    api = next(r for r in demo_scan.resources if (r.name or "") == "api-1")
    against_api = [f for f in demo_scan.findings if f.resource_id == api.resource_id]
    assert any(finding.category == "idle" for finding in against_api)


def test_the_scan_is_readable_from_the_store(demo_run):
    scan, store = demo_run
    assert store.latest_scan_id() == scan.scan_id
    reloaded = store.get_scan(scan.scan_id)
    assert reloaded is not None
    assert len(reloaded.resources) == len(scan.resources)
    assert reloaded.tco is not None


def test_synthetic_costs_are_labelled_as_unavailable(demo_scan):
    capabilities = {note.capability: note for note in demo_scan.notes}
    assert capabilities["ce:GetCostAndUsage"].status == "unavailable"
    assert "synthetic" in capabilities["ce:GetCostAndUsage"].message
    assert capabilities["cloudwatch:GetMetricData"].status == "unavailable"


def test_demo_cost_snapshot_is_internally_consistent():
    snapshot = demo_cost_snapshot(30)
    assert snapshot.days_in_period == 30
    assert snapshot.total_cost == pytest.approx(sum(snapshot.daily_totals.values()), abs=0.05)
    assert len(snapshot.daily_totals) == 30
    assert snapshot.monthly_run_rate > snapshot.total_cost * 0.9


def test_instance_prices_scale_with_size_and_family():
    assert _instance_hourly("m5.large") == pytest.approx(0.0832, abs=0.001)
    assert _instance_hourly("m5.2xlarge") > _instance_hourly("m5.xlarge")
    # Graviton lists below its x86 sibling, and burstable below general purpose.
    assert _instance_hourly("m6g.xlarge") < _instance_hourly("m5.xlarge")
    assert _instance_hourly("t3.large") < _instance_hourly("m5.large")
    assert _instance_hourly("nonsense") is None
