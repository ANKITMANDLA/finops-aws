from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from tests.factories import make_finding, make_resource, make_scan

from finops.api import create_app
from finops.config import Settings
from finops.model import Advice, ArchitectureRecommendation, CapabilityNote, CostRecord, TcoReport
from finops.store import ScanStore


@pytest.fixture
def store(tmp_path) -> ScanStore:
    return ScanStore(tmp_path / "finops.db")


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(db_path=tmp_path / "finops.db", llm_provider="none")


class StubRunner:
    """Stands in for the real runner so tests never touch AWS."""

    def __init__(self):
        self.started_with = None
        self.running = False

    def start(self, options):
        self.started_with = options
        return {"job_id": "job1", "status": "queued", "stage": "queued", "log": []}

    def status(self):
        return {"job_id": "job1", "status": "running", "stage": "inventory", "log": []}


@pytest.fixture
def client(store, settings) -> TestClient:
    app = create_app(store, settings, runner=StubRunner(), static_dir=None)
    return TestClient(app)


def seeded_scan(store: ScanStore, scan_id="20260801T120000Z-aaaa", **kwargs):
    scan = make_scan(scan_id=scan_id, **kwargs)
    scan.tco = TcoReport(
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        total_cost=3000.0,
        monthly_run_rate=3044.0,
        identified_monthly_savings=300.0,
        savings_percent=9.86,
    )
    scan.notes = [
        CapabilityNote(
            capability="trusted-advisor", status="denied", message="Business support required"
        )
    ]
    store.save_scan(scan)
    return scan


def test_health_reports_the_store_and_provider(client, store):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["latest_scan_id"] is None
    assert body["llm_provider"] == "none"

    seeded_scan(store)
    assert client.get("/api/health").json()["latest_scan_id"] == "20260801T120000Z-aaaa"


def test_endpoints_explain_themselves_when_there_are_no_scans(client):
    response = client.get("/api/scans/latest")
    assert response.status_code == 404
    assert "finops scan" in response.json()["detail"]


def test_latest_resolves_to_the_most_recent_scan(client, store):
    seeded_scan(store, scan_id="20260801T120000Z-aaaa")
    seeded_scan(store, scan_id="20260802T120000Z-bbbb")

    body = client.get("/api/scans/latest").json()
    assert body["meta"]["scan_id"] == "20260802T120000Z-bbbb"
    assert body["tco"]["monthly_run_rate"] == 3044.0
    assert body["notes"][0]["capability"] == "trusted-advisor"


def test_unknown_scan_id_is_a_404(client, store):
    seeded_scan(store)
    assert client.get("/api/scans/nope").status_code == 404


def test_resources_are_paginated_and_filterable(client, store):
    resources = [
        make_resource("i-1", service="EC2", region="us-east-1", monthly_cost=100.0),
        make_resource("i-2", service="EC2", region="eu-west-1", monthly_cost=50.0),
        make_resource("vol-1", service="EBS", region="us-east-1", monthly_cost=10.0),
    ]
    resources[2].resource_type = "ebs:volume"
    seeded_scan(store, resources=resources)

    everything = client.get("/api/scans/latest/resources").json()
    assert everything["total"] == 3
    # Most expensive first, so the drill-in list is useful without sorting.
    assert everything["items"][0]["resource_id"] == "i-1"

    ec2 = client.get("/api/scans/latest/resources", params={"service": "EC2"}).json()
    assert ec2["total"] == 2

    page = client.get("/api/scans/latest/resources", params={"limit": 1, "offset": 1}).json()
    assert page["total"] == 3 and len(page["items"]) == 1


def test_single_resource_is_fetched_by_arn_query_parameter(client, store):
    resource = make_resource("i-1")
    seeded_scan(store, resources=[resource])

    found = client.get("/api/scans/latest/resource", params={"arn": resource.arn})
    assert found.status_code == 200
    assert found.json()["resource_id"] == "i-1"

    assert (
        client.get(
            "/api/scans/latest/resource", params={"arn": "arn:aws:ec2:::instance/nope"}
        ).status_code
        == 404
    )


def test_findings_can_be_filtered_by_category_and_savings(client, store):
    findings = [
        make_finding("idle_ec2", savings=100.0, category="idle", resource_arn="arn:1"),
        make_finding("gp2", savings=5.0, category="storage", resource_arn="arn:2"),
    ]
    seeded_scan(store, findings=findings)

    assert client.get("/api/scans/latest/findings").json()["total"] == 2
    assert (
        client.get("/api/scans/latest/findings", params={"category": "idle"}).json()["total"] == 1
    )
    assert client.get("/api/scans/latest/findings", params={"min_savings": 50}).json()["total"] == 1


def test_filter_options_come_from_the_scan_itself(client, store):
    seeded_scan(
        store,
        resources=[
            make_resource("i-1", service="EC2", region="us-east-1"),
            make_resource("b-1", service="S3", region="eu-west-1"),
        ],
        findings=[make_finding("idle_ec2", category="idle", resource_arn="arn:1")],
    )

    filters = client.get("/api/scans/latest/filters").json()
    assert filters["services"] == ["EC2", "S3"]
    assert filters["regions"] == ["eu-west-1", "us-east-1"]
    assert filters["finding_categories"] == ["idle"]


def test_costs_can_be_narrowed_to_one_grouping(client, store):
    costs = [
        CostRecord(
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 2),
            granularity="DAILY",
            amount=10.0,
            dimensions={"SERVICE": "Amazon EC2"},
        ),
        CostRecord(
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 2),
            granularity="DAILY",
            amount=4.0,
            dimensions={"REGION": "us-east-1"},
        ),
    ]
    seeded_scan(store, costs=costs)

    assert len(client.get("/api/scans/latest/costs").json()) == 2
    by_service = client.get("/api/scans/latest/costs", params={"group_by": "SERVICE"}).json()
    assert len(by_service) == 1 and by_service[0]["dimensions"]["SERVICE"] == "Amazon EC2"


def test_advice_is_served_when_present_and_404s_when_not(client, store):
    seeded_scan(store)
    assert client.get("/api/scans/latest/advice").status_code == 404

    advice = Advice(
        provider="bedrock",
        model="claude",
        executive_summary="Spend is concentrated in EC2.",
        recommendations=[
            ArchitectureRecommendation(title="Consolidate NAT", summary="One gateway", rationale="")
        ],
    )
    store.save_advice("20260801T120000Z-aaaa", advice)

    body = client.get("/api/scans/latest/advice").json()
    assert body["provider"] == "bedrock"
    assert body["recommendations"][0]["title"] == "Consolidate NAT"


def test_advice_can_be_regenerated_without_rescanning(client, store, settings):
    seeded_scan(store, findings=[make_finding("idle_ec2", savings=42.0, resource_arn="arn:1")])

    body = client.post("/api/scans/latest/advice").json()

    # No provider is configured, so this is the deterministic fallback - which still
    # has to be persisted and returned rather than erroring out.
    assert body["provider"] == "none"
    assert body["executive_summary"]
    assert store.get_advice("20260801T120000Z-aaaa") is not None


def test_chat_capabilities_describe_the_tools_without_connecting(client):
    body = client.get("/api/chat/capabilities").json()

    assert body["provider"] == "none"
    assert {server["key"] for server in body["servers"]} == {"aws", "pricing"}
    assert any(tool["name"] == "finops_search_findings" for tool in body["scan_tools"])


def test_chat_without_a_provider_answers_with_an_error_not_a_500(client, store):
    seeded_scan(store)

    response = client.post(
        "/api/scans/latest/chat", json={"messages": [{"role": "user", "content": "hello"}]}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["message"] == ""
    assert "FINOPS_LLM_PROVIDER" in body["error"]


def test_chat_needs_a_scan_to_talk_about(client):
    response = client.post(
        "/api/scans/latest/chat", json={"messages": [{"role": "user", "content": "hello"}]}
    )
    assert response.status_code == 404


def test_chat_rejects_an_empty_conversation(client, store):
    seeded_scan(store)
    assert client.post("/api/scans/latest/chat", json={"messages": []}).status_code == 422


def test_starting_a_scan_returns_immediately_with_a_job_handle(client):
    response = client.post("/api/scans", json={"regions": ["us-east-1"], "with_advice": False})
    assert response.status_code == 202
    assert response.json()["job_id"] == "job1"

    runner: StubRunner = client.app.state.runner
    assert runner.started_with.regions == ["us-east-1"]
    assert runner.started_with.with_advice is False


def test_a_second_concurrent_scan_is_rejected(store, settings):
    class BusyRunner(StubRunner):
        def start(self, options):
            from finops.jobs import ScanAlreadyRunning

            raise ScanAlreadyRunning("Scan abc is already running (inventory)")

    client = TestClient(create_app(store, settings, runner=BusyRunner(), static_dir=None))
    response = client.post("/api/scans")
    assert response.status_code == 409
    assert "already running" in response.json()["detail"]


def test_scan_status_is_available_while_a_scan_runs(client):
    assert client.get("/api/scans/status").json()["stage"] == "inventory"


def test_trends_are_oldest_first_for_the_chart(client, store):
    seeded_scan(store, scan_id="20260801T120000Z-aaaa")
    seeded_scan(store, scan_id="20260802T120000Z-bbbb")

    trend = client.get("/api/trends").json()
    assert [item["scan_id"] for item in trend] == [
        "20260801T120000Z-aaaa",
        "20260802T120000Z-bbbb",
    ]


def test_compare_defaults_to_the_previous_scan(client, store):
    first = seeded_scan(store, scan_id="20260801T120000Z-aaaa")
    first.tco.monthly_run_rate = 1000.0
    store.save_scan(first)
    second = seeded_scan(store, scan_id="20260802T120000Z-bbbb")
    second.tco.monthly_run_rate = 1200.0
    store.save_scan(second)

    body = client.get("/api/scans/latest/compare").json()
    assert body["baseline_scan_id"] == "20260801T120000Z-aaaa"
    assert body["run_rate_change"] == 200.0
    assert body["run_rate_change_percent"] == 20.0


def test_compare_on_a_first_scan_has_no_baseline(client, store):
    seeded_scan(store)
    body = client.get("/api/scans/latest/compare").json()
    assert body["baseline_scan_id"] is None
    assert body["run_rate_change"] is None


def test_deleting_a_scan_removes_its_rows(client, store):
    seeded_scan(store, resources=[make_resource("i-1")])
    assert client.delete("/api/scans/latest").status_code == 204
    assert store.list_scans() == []


def test_cors_allows_the_vite_dev_server(client):
    response = client.get("/api/health", headers={"Origin": "http://localhost:5173"})
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_spa_fallback_serves_index_for_unknown_paths(store, settings, tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>dashboard</html>", encoding="utf-8")

    client = TestClient(create_app(store, settings, runner=StubRunner(), static_dir=dist))

    assert client.get("/savings").text == "<html>dashboard</html>"
    # API routes still win over the catch-all.
    assert client.get("/api/health").json()["status"] == "ok"


def test_openapi_schema_builds(client):
    # A malformed response_model only shows up when the schema is generated.
    schema = client.get("/openapi.json")
    assert schema.status_code == 200
    assert "/api/scans/{scan_id}/tco" in schema.json()["paths"]
