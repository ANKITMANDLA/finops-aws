from __future__ import annotations

from datetime import UTC, date

import pytest
from tests.factories import make_finding, make_resource

from finops import pipeline
from finops.aws.costs import CostSnapshot
from finops.aws.session import CredentialsUnavailable
from finops.config import Settings
from finops.jobs import ScanAlreadyRunning, ScanRunner
from finops.model import Advice, make_finding_id
from finops.pipeline import ScanOptions, run_scan
from finops.store import ScanStore


class FakeAws:
    settings = Settings()
    regions = ["us-east-1", "eu-west-1"]
    default_region = "us-east-1"
    account_id = "111122223333"
    account_alias = "sandbox"

    def verify_credentials(self):
        return None

    def client(self, service, region=None):
        raise AssertionError(f"pipeline should not open a real {service} client in tests")


@pytest.fixture
def wired(monkeypatch):
    """Replace every AWS-touching stage with a recorder."""
    calls: dict[str, object] = {}

    snapshot = CostSnapshot(
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        total_cost=3000.0,
        service_totals={"Amazon EC2": 3000.0},
    )

    resources = [make_resource("i-1", monthly_cost=None), make_resource("i-2", monthly_cost=None)]

    def fake_collect(ctx, **kwargs):
        calls["collect"] = kwargs
        return list(resources)

    class FakeExplorer:
        def __init__(self, aws, notes=None):
            pass

        def snapshot(self, lookback_days):
            calls["lookback"] = lookback_days
            return snapshot

    class FakeMetrics:
        def __init__(self, aws, notes=None):
            pass

        def collect(self, resources):
            calls["metrics"] = len(list(resources))
            return resources

    class FakeNative:
        def __init__(self, aws, notes=None):
            pass

        def collect(self, regions):
            calls["native_regions"] = list(regions)
            return [make_finding("compute-optimizer", savings=25.0, source="compute-optimizer")]

    class FakePricing:
        pass

    def fake_build_pricing(aws, notes=None, cache_path=None):
        return FakePricing()

    def fake_attribute(resources, snapshot, pricing):
        calls["attributed"] = True
        for resource in resources:
            resource.monthly_cost = 500.0
            resource.cost_basis = "list_price_estimate"
        return list(resources)

    def fake_run_rules(ctx, only=None, skip=None):
        calls["rules"] = {"only": only, "skip": skip}
        return [make_finding("idle_ec2", savings=100.0, resource_arn="arn:other")]

    monkeypatch.setattr(pipeline, "collect_inventory", fake_collect)
    monkeypatch.setattr(pipeline, "CostExplorer", FakeExplorer)
    monkeypatch.setattr(pipeline, "MetricsCollector", FakeMetrics)
    monkeypatch.setattr(pipeline, "NativeRecommendations", FakeNative)
    monkeypatch.setattr(pipeline, "build_pricing", fake_build_pricing)
    monkeypatch.setattr(pipeline, "attribute_costs", fake_attribute)
    monkeypatch.setattr(pipeline, "run_rules", fake_run_rules)
    return calls


class StubAdvisor:
    def __init__(self):
        self.called_with = None

    def advise(self, report, findings, resources, notes=None):
        self.called_with = (report, list(findings), list(resources))
        return Advice(provider="stub", executive_summary="ok")


def test_scan_produces_a_complete_report(wired, tmp_path):
    settings = Settings(db_path=tmp_path / "finops.db")
    store = ScanStore(settings.db_path)

    scan = run_scan(settings, ScanOptions(), aws=FakeAws(), store=store, advisor=StubAdvisor())

    assert scan.account_id == "111122223333"
    assert len(scan.resources) == 2
    assert scan.tco is not None and scan.tco.total_cost == 3000.0
    assert scan.advice is not None and scan.advice.provider == "stub"
    assert scan.duration_seconds >= 0
    assert scan.finished_at is not None


def test_native_and_rule_findings_are_merged_and_ranked(wired, tmp_path):
    scan = run_scan(
        Settings(db_path=tmp_path / "finops.db"),
        ScanOptions(persist=False),
        aws=FakeAws(),
        advisor=StubAdvisor(),
    )

    assert [f.estimated_monthly_savings for f in scan.findings] == [100.0, 25.0]
    assert scan.tco.identified_monthly_savings == 125.0


def test_duplicate_findings_from_aws_and_our_rules_are_not_double_counted(
    wired, monkeypatch, tmp_path
):
    # Both sources describe the same action on the same resource.
    duplicate = make_finding("idle_ec2", savings=100.0, resource_arn="arn:same")
    duplicate.id = make_finding_id("terminate", "arn:same")
    monkeypatch.setattr(pipeline, "run_rules", lambda ctx, only=None, skip=None: [duplicate])

    native = make_finding("compute-optimizer", savings=80.0, resource_arn="arn:same")
    native.id = make_finding_id("terminate", "arn:same")
    native.source = "compute-optimizer"

    class OneNative:
        def __init__(self, aws, notes=None):
            pass

        def collect(self, regions):
            return [native]

    monkeypatch.setattr(pipeline, "NativeRecommendations", OneNative)

    scan = run_scan(
        Settings(db_path=tmp_path / "finops.db"),
        ScanOptions(persist=False),
        aws=FakeAws(),
        advisor=StubAdvisor(),
    )

    assert len(scan.findings) == 1
    assert scan.tco.identified_monthly_savings == 80.0  # AWS's own estimate wins


def test_the_scan_is_persisted_and_readable_afterwards(wired, tmp_path):
    settings = Settings(db_path=tmp_path / "finops.db")
    store = ScanStore(settings.db_path)

    scan = run_scan(settings, ScanOptions(), aws=FakeAws(), store=store, advisor=StubAdvisor())

    assert store.latest_scan_id() == scan.scan_id
    reloaded = store.get_scan(scan.scan_id)
    assert reloaded is not None
    assert len(reloaded.resources) == 2
    assert reloaded.advice.provider == "stub"


def test_persist_can_be_turned_off(wired, tmp_path):
    settings = Settings(db_path=tmp_path / "finops.db")
    store = ScanStore(settings.db_path)
    run_scan(
        settings, ScanOptions(persist=False), aws=FakeAws(), store=store, advisor=StubAdvisor()
    )
    assert store.list_scans() == []


def test_stages_can_be_skipped(wired, tmp_path):
    scan = run_scan(
        Settings(db_path=tmp_path / "finops.db"),
        ScanOptions(persist=False, with_metrics=False, with_native=False, with_advice=False),
        aws=FakeAws(),
    )

    assert "metrics" not in wired
    assert "native_regions" not in wired
    assert scan.advice is None
    assert all(f.source == "rules" for f in scan.findings)


def test_region_scope_is_passed_to_every_stage(wired, tmp_path):
    scan = run_scan(
        Settings(db_path=tmp_path / "finops.db"),
        ScanOptions(regions=["eu-west-1"], persist=False),
        aws=FakeAws(),
        advisor=StubAdvisor(),
    )

    assert scan.regions == ["eu-west-1"]
    assert wired["collect"]["regions"] == ["eu-west-1"]
    assert wired["native_regions"] == ["eu-west-1"]


def test_rule_selection_is_forwarded(wired, tmp_path):
    run_scan(
        Settings(db_path=tmp_path / "finops.db"),
        ScanOptions(rules=["ebs.gp2_to_gp3"], skip_rules=["ec2.idle"], persist=False),
        aws=FakeAws(),
        advisor=StubAdvisor(),
    )
    assert wired["rules"] == {"only": ["ebs.gp2_to_gp3"], "skip": ["ec2.idle"]}


def test_findings_below_the_noise_floor_are_dropped(wired, monkeypatch, tmp_path):
    monkeypatch.setattr(
        pipeline,
        "run_rules",
        lambda ctx, only=None, skip=None: [
            make_finding("tiny", savings=0.10, resource_arn="arn:tiny")
        ],
    )
    settings = Settings(db_path=tmp_path / "finops.db")
    settings.thresholds.min_monthly_savings_usd = 1.0

    scan = run_scan(
        settings,
        ScanOptions(persist=False, with_native=False),
        aws=FakeAws(),
        advisor=StubAdvisor(),
    )

    assert scan.findings == []


def test_advisor_receives_the_report_and_the_findings(wired, tmp_path):
    advisor = StubAdvisor()
    run_scan(
        Settings(db_path=tmp_path / "finops.db"),
        ScanOptions(persist=False),
        aws=FakeAws(),
        advisor=advisor,
    )
    report, findings, resources = advisor.called_with
    assert report.total_cost == 3000.0
    assert len(findings) == 2
    assert len(resources) == 2


def test_progress_callback_reports_each_stage(wired, tmp_path):
    seen = []
    run_scan(
        Settings(db_path=tmp_path / "finops.db"),
        ScanOptions(persist=False),
        aws=FakeAws(),
        advisor=StubAdvisor(),
        progress=lambda stage, message: seen.append(stage),
    )
    for stage in ("inventory", "costs", "metrics", "pricing", "native", "rules", "tco", "advice"):
        assert stage in seen


def test_scan_ids_sort_chronologically():
    from datetime import datetime

    early = pipeline._new_scan_id(datetime(2026, 1, 1, tzinfo=UTC))
    late = pipeline._new_scan_id(datetime(2026, 6, 1, tzinfo=UTC))
    assert early < late
    assert early.startswith("20260101T000000Z-")


def test_scan_refuses_to_run_without_credentials(wired, tmp_path):
    """A credential-less scan must fail, not report an account with nothing in it."""
    settings = Settings(db_path=tmp_path / "finops.db")
    store = ScanStore(settings.db_path)

    class Unauthenticated(FakeAws):
        def verify_credentials(self):
            raise CredentialsUnavailable("Unable to authenticate to AWS.")

    with pytest.raises(CredentialsUnavailable):
        run_scan(settings, ScanOptions(), aws=Unauthenticated(), store=store)

    assert store.list_scans() == []


def test_regenerate_advice_reuses_the_stored_scan(wired, tmp_path):
    settings = Settings(db_path=tmp_path / "finops.db")
    store = ScanStore(settings.db_path)
    scan = run_scan(settings, ScanOptions(), aws=FakeAws(), store=store, advisor=StubAdvisor())

    class SecondAdvisor(StubAdvisor):
        def advise(self, report, findings, resources, notes=None):
            return Advice(provider="second", executive_summary="new take")

    pipeline.regenerate_advice(scan, settings, store=store, advisor=SecondAdvisor())

    assert store.get_advice(scan.scan_id).provider == "second"


# --- background jobs ----------------------------------------------------------------


def test_job_runner_reports_progress_then_success(wired, tmp_path, monkeypatch):
    settings = Settings(db_path=tmp_path / "finops.db")
    store = ScanStore(settings.db_path)

    real_run_scan = pipeline.run_scan

    def patched(settings_, options, **kwargs):
        return real_run_scan(settings_, options, aws=FakeAws(), advisor=StubAdvisor(), **kwargs)

    monkeypatch.setattr("finops.jobs.run_scan", patched)

    runner = ScanRunner(store, settings)
    handle = runner.start(ScanOptions())
    assert handle["status"] == "queued"
    runner.wait(timeout=30)

    status = runner.status()
    assert status["status"] == "succeeded"
    assert status["scan_id"] == store.latest_scan_id()
    assert any("inventory" in line for line in status["log"])
    assert not runner.is_running


def test_job_runner_records_a_failure_instead_of_dying(tmp_path, monkeypatch):
    settings = Settings(db_path=tmp_path / "finops.db")

    def explode(*args, **kwargs):
        raise RuntimeError("credentials expired")

    monkeypatch.setattr("finops.jobs.run_scan", explode)

    runner = ScanRunner(ScanStore(settings.db_path), settings)
    runner.start()
    runner.wait(timeout=30)

    status = runner.status()
    assert status["status"] == "failed"
    assert "credentials expired" in status["error"]


def test_only_one_scan_runs_at_a_time(tmp_path, monkeypatch):
    import threading

    release = threading.Event()

    def slow(*args, **kwargs):
        release.wait(timeout=10)
        raise RuntimeError("stopped")

    monkeypatch.setattr("finops.jobs.run_scan", slow)
    settings = Settings(db_path=tmp_path / "finops.db")
    runner = ScanRunner(ScanStore(settings.db_path), settings)

    runner.start()
    with pytest.raises(ScanAlreadyRunning):
        runner.start()
    release.set()
    runner.wait(timeout=15)
