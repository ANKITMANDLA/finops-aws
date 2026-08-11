from __future__ import annotations

import pytest
from tests.factories import make_scan
from typer.testing import CliRunner

from finops import cli
from finops.config import Settings
from finops.store import ScanStore

runner = CliRunner()


@pytest.fixture
def store(tmp_path, monkeypatch) -> ScanStore:
    """A store seeded with one live scan, one demo scan, and one failed scan."""
    settings = Settings(db_path=tmp_path / "finops.db", llm_provider="none", _env_file=None)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)

    store = ScanStore(settings.db_path)
    live = make_scan("scan-live")
    live.started_at = live.started_at.replace(year=2026)
    store.save_scan(live)

    demo = make_scan("scan-demo")
    demo.dry_run = True
    demo.started_at = demo.started_at.replace(year=2025)
    store.save_scan(demo)

    failed = make_scan("scan-empty", resources=[], findings=[])
    failed.started_at = failed.started_at.replace(year=2024)
    store.save_scan(failed)
    return store


def ids(store: ScanStore) -> set[str]:
    return {meta.scan_id for meta in store.list_scans()}


def test_prune_demo_removes_only_dry_run_scans(store):
    result = runner.invoke(cli.app, ["prune", "--demo", "--yes"])

    assert result.exit_code == 0
    assert ids(store) == {"scan-live", "scan-empty"}


def test_prune_empty_removes_scans_that_collected_nothing(store):
    result = runner.invoke(cli.app, ["prune", "--empty", "--yes"])

    assert result.exit_code == 0
    assert ids(store) == {"scan-live", "scan-demo"}


def test_prune_combines_filters(store):
    result = runner.invoke(cli.app, ["prune", "--demo", "--empty", "--yes"])

    assert result.exit_code == 0
    assert ids(store) == {"scan-live"}


def test_prune_by_id(store):
    result = runner.invoke(cli.app, ["prune", "--id", "scan-live", "--yes"])

    assert result.exit_code == 0
    assert ids(store) == {"scan-demo", "scan-empty"}


def test_declining_the_prompt_deletes_nothing(store):
    result = runner.invoke(cli.app, ["prune", "--demo"], input="n\n")

    assert result.exit_code == 0
    assert ids(store) == {"scan-live", "scan-demo", "scan-empty"}


def test_bare_prune_still_keeps_the_ten_most_recent(store):
    result = runner.invoke(cli.app, ["prune", "--yes"])

    assert result.exit_code == 0
    assert "Nothing to prune" in result.stdout
    assert len(ids(store)) == 3


def test_keep_applies_after_the_other_filters(store):
    """--demo --keep 1 means: drop the demos, then keep the newest of what is left."""
    result = runner.invoke(cli.app, ["prune", "--demo", "--keep", "1", "--yes"])

    assert result.exit_code == 0
    assert ids(store) == {"scan-live"}


def test_scans_listing_labels_each_kind(store):
    result = runner.invoke(cli.app, ["scans"])

    assert result.exit_code == 0
    assert "demo" in result.stdout
    assert "empty" in result.stdout
    assert "live" in result.stdout
