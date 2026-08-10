from __future__ import annotations

from tests.factories import make_finding, make_resource, make_scan

from finops.model import Advice, ArchitectureRecommendation, CapabilityNote
from finops.store import ScanStore, summarize_costs


def test_save_and_hydrate_round_trip(tmp_path):
    store = ScanStore(tmp_path / "finops.db")
    scan = make_scan()
    scan.notes.append(
        CapabilityNote(
            capability="compute-optimizer",
            status="not_enrolled",
            message="Account is not opted in.",
        )
    )

    store.save_scan(scan)
    loaded = store.get_scan(scan.scan_id)

    assert loaded is not None
    assert loaded.account_id == "111122223333"
    assert loaded.regions == ["us-east-1", "us-west-2"]
    assert len(loaded.resources) == 1
    assert loaded.resources[0].resource_id == "i-0123456789abcdef0"
    assert len(loaded.findings) == 1
    assert loaded.findings[0].evidence[0].label == "Average CPU"
    assert loaded.findings[0].remediation is not None
    assert loaded.findings[0].rollback_possible is False
    assert loaded.tco is not None and loaded.tco.total_cost == 942.0
    assert loaded.notes[0].status == "not_enrolled"


def test_saving_same_scan_id_replaces_previous_rows(tmp_path):
    store = ScanStore(tmp_path / "finops.db")
    store.save_scan(make_scan(resources=[make_resource(), make_resource("i-second")]))
    store.save_scan(make_scan(resources=[make_resource()]))

    loaded = store.get_scan("scan-test-1")
    assert loaded is not None
    assert len(loaded.resources) == 1
    assert len(store.get_costs("scan-test-1")) == 1


def test_query_resources_filters_and_paginates(tmp_path):
    store = ScanStore(tmp_path / "finops.db")
    resources = [
        make_resource("i-a", monthly_cost=10.0),
        make_resource("i-b", monthly_cost=300.0),
        make_resource("vol-a", resource_type="ebs:volume", service="EBS", monthly_cost=8.0),
        make_resource("i-c", region="us-west-2", monthly_cost=50.0),
    ]
    store.save_scan(make_scan(resources=resources))

    ec2_only, total = store.query_resources("scan-test-1", service="EC2")
    assert total == 3
    # Highest monthly cost first.
    assert [r.resource_id for r in ec2_only] == ["i-b", "i-c", "i-a"]

    west, total_west = store.query_resources("scan-test-1", region="us-west-2")
    assert total_west == 1 and west[0].resource_id == "i-c"

    page, total_all = store.query_resources("scan-test-1", limit=2, offset=1)
    assert total_all == 4 and len(page) == 2

    found, _ = store.query_resources("scan-test-1", search="vol-")
    assert [r.resource_id for r in found] == ["vol-a"]


def test_query_findings_sorted_by_savings_and_filtered(tmp_path):
    store = ScanStore(tmp_path / "finops.db")
    findings = [
        make_finding("ec2.idle_instance", savings=10.0, resource_arn="arn:aws:ec2:::instance/i-1"),
        make_finding(
            "ebs.unattached",
            savings=200.0,
            category="storage",
            service="EBS",
            resource_arn="arn:aws:ec2:::volume/vol-1",
        ),
        make_finding(
            "co.rightsize",
            savings=75.0,
            source="compute-optimizer",
            resource_arn="arn:aws:ec2:::instance/i-2",
        ),
    ]
    store.save_scan(make_scan(findings=findings))

    ranked, total = store.query_findings("scan-test-1")
    assert total == 3
    assert [f.estimated_monthly_savings for f in ranked] == [200.0, 75.0, 10.0]

    storage, _ = store.query_findings("scan-test-1", category="storage")
    assert [f.rule_id for f in storage] == ["ebs.unattached"]

    native, _ = store.query_findings("scan-test-1", source="compute-optimizer")
    assert [f.rule_id for f in native] == ["co.rightsize"]

    big, _ = store.query_findings("scan-test-1", min_savings=50.0)
    assert len(big) == 2


def test_advice_is_saved_separately_after_the_scan(tmp_path):
    store = ScanStore(tmp_path / "finops.db")
    store.save_scan(make_scan())
    assert store.get_advice("scan-test-1") is None

    advice = Advice(
        provider="bedrock",
        model="claude",
        executive_summary="Spend is concentrated in EC2.",
        recommendations=[
            ArchitectureRecommendation(
                title="Consolidate NAT Gateways",
                summary="Use one NAT Gateway per AZ only where required.",
                rationale="Three gateways serve low-traffic subnets.",
            )
        ],
    )
    store.save_advice("scan-test-1", advice)

    loaded = store.get_advice("scan-test-1")
    assert loaded is not None
    assert loaded.provider == "bedrock"
    assert loaded.recommendations[0].title == "Consolidate NAT Gateways"


def test_trend_returns_scans_oldest_first_and_prune_keeps_recent(tmp_path):
    store = ScanStore(tmp_path / "finops.db")
    for index in range(5):
        scan = make_scan(f"scan-{index}")
        scan.started_at = scan.started_at.replace(year=2020 + index)
        store.save_scan(scan)

    assert [m.scan_id for m in store.trend()] == [f"scan-{i}" for i in range(5)]
    assert store.latest_scan_id() == "scan-4"
    assert store.resolve_scan_id("latest") == "scan-4"

    removed = store.prune(keep=2)
    assert removed == 3
    assert {m.scan_id for m in store.list_scans()} == {"scan-3", "scan-4"}


def test_distinct_values_rejects_unknown_columns(tmp_path):
    store = ScanStore(tmp_path / "finops.db")
    store.save_scan(make_scan(resources=[make_resource(), make_resource("vol-a", service="EBS")]))

    assert store.distinct_values("scan-test-1", "resources", "service") == ["EBS", "EC2"]
    try:
        store.distinct_values("scan-test-1", "resources", "arn; DROP TABLE resources")
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected ValueError for a non-allowlisted column")


def test_summarize_costs_groups_by_dimension():
    scan = make_scan()
    totals = summarize_costs(scan.costs, "SERVICE")
    assert totals == {"Amazon Elastic Compute Cloud - Compute": 31.4}
    assert summarize_costs(scan.costs, "REGION") == {}
