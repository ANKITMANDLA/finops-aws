"""SQLite persistence for scans.

Scans are normalized into ``resources``, ``findings``, and ``costs`` tables so the API
can filter and aggregate without loading an entire scan into memory, while report-level
objects (TCO, advice, capability notes) are kept as JSON on the ``scans`` row.

A fresh connection is opened per operation, which keeps the store safe to use from
FastAPI's thread pool without juggling per-thread connection state.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from finops.model import (
    Advice,
    CapabilityNote,
    CostRecord,
    Finding,
    Resource,
    Scan,
    ScanMeta,
    TcoReport,
)

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    scan_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    account_alias TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    duration_seconds REAL NOT NULL DEFAULT 0,
    regions_json TEXT NOT NULL DEFAULT '[]',
    resource_count INTEGER NOT NULL DEFAULT 0,
    finding_count INTEGER NOT NULL DEFAULT 0,
    monthly_run_rate REAL NOT NULL DEFAULT 0,
    identified_monthly_savings REAL NOT NULL DEFAULT 0,
    tco_json TEXT,
    advice_json TEXT,
    notes_json TEXT NOT NULL DEFAULT '[]',
    dry_run INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS resources (
    scan_id TEXT NOT NULL REFERENCES scans(scan_id) ON DELETE CASCADE,
    arn TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    service TEXT NOT NULL,
    region TEXT NOT NULL,
    account_id TEXT NOT NULL,
    name TEXT,
    availability_zone TEXT,
    state TEXT,
    created_at TEXT,
    monthly_cost REAL,
    cost_basis TEXT,
    tags_json TEXT NOT NULL DEFAULT '{}',
    attributes_json TEXT NOT NULL DEFAULT '{}',
    metrics_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (scan_id, arn)
);
CREATE INDEX IF NOT EXISTS idx_resources_scan_service ON resources(scan_id, service);
CREATE INDEX IF NOT EXISTS idx_resources_scan_region ON resources(scan_id, region);
CREATE INDEX IF NOT EXISTS idx_resources_scan_cost ON resources(scan_id, monthly_cost DESC);

CREATE TABLE IF NOT EXISTS findings (
    scan_id TEXT NOT NULL REFERENCES scans(scan_id) ON DELETE CASCADE,
    finding_id TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    action_type TEXT NOT NULL,
    service TEXT NOT NULL,
    source TEXT NOT NULL,
    resource_arn TEXT,
    resource_id TEXT,
    resource_type TEXT,
    region TEXT,
    estimated_monthly_savings REAL NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'USD',
    confidence TEXT NOT NULL,
    implementation_effort TEXT NOT NULL,
    risk TEXT NOT NULL,
    cost_basis TEXT NOT NULL,
    rollback_possible INTEGER NOT NULL DEFAULT 1,
    detail TEXT,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    remediation_json TEXT,
    tags_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (scan_id, finding_id)
);
CREATE INDEX IF NOT EXISTS idx_findings_scan_savings
    ON findings(scan_id, estimated_monthly_savings DESC);
CREATE INDEX IF NOT EXISTS idx_findings_scan_category ON findings(scan_id, category);

CREATE TABLE IF NOT EXISTS costs (
    scan_id TEXT NOT NULL REFERENCES scans(scan_id) ON DELETE CASCADE,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    granularity TEXT NOT NULL,
    metric TEXT NOT NULL,
    amount REAL NOT NULL,
    unit TEXT NOT NULL DEFAULT 'USD',
    dim_service TEXT,
    dim_region TEXT,
    dim_usage_type TEXT,
    dimensions_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_costs_scan ON costs(scan_id, granularity);
CREATE INDEX IF NOT EXISTS idx_costs_scan_service ON costs(scan_id, dim_service);
"""


def _dumps(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class ScanStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        with self._connect() as conn:
            # WAL lets the dashboard read while a scan writes.
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(_SCHEMA)
            _migrate(conn)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # ------------------------------------------------------------------ writes

    def save_scan(self, scan: Scan) -> None:
        meta = scan.meta
        with self._connect() as conn:
            conn.execute("DELETE FROM scans WHERE scan_id = ?", (scan.scan_id,))
            conn.execute(
                """
                INSERT INTO scans (
                    scan_id, account_id, account_alias, started_at, finished_at,
                    duration_seconds, regions_json, resource_count, finding_count,
                    monthly_run_rate, identified_monthly_savings, tco_json, advice_json,
                    notes_json, dry_run
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    scan.scan_id,
                    scan.account_id,
                    scan.account_alias,
                    _iso(scan.started_at),
                    _iso(scan.finished_at),
                    scan.duration_seconds,
                    _dumps(scan.regions),
                    meta.resource_count,
                    meta.finding_count,
                    meta.monthly_run_rate,
                    meta.identified_monthly_savings,
                    scan.tco.model_dump_json() if scan.tco else None,
                    scan.advice.model_dump_json() if scan.advice else None,
                    _dumps([n.model_dump(mode="json") for n in scan.notes]),
                    int(scan.dry_run),
                ),
            )
            conn.executemany(
                """
                INSERT OR REPLACE INTO resources (
                    scan_id, arn, resource_id, resource_type, service, region, account_id,
                    name, availability_zone, state, created_at, monthly_cost, cost_basis,
                    tags_json, attributes_json, metrics_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        scan.scan_id,
                        r.arn,
                        r.resource_id,
                        r.resource_type,
                        r.service,
                        r.region,
                        r.account_id,
                        r.name,
                        r.availability_zone,
                        r.state,
                        _iso(r.created_at),
                        r.monthly_cost,
                        r.cost_basis,
                        _dumps(r.tags),
                        _dumps(r.attributes),
                        _dumps(r.metrics),
                    )
                    for r in scan.resources
                ],
            )
            conn.executemany(
                """
                INSERT OR REPLACE INTO findings (
                    scan_id, finding_id, rule_id, title, category, action_type, service,
                    source, resource_arn, resource_id, resource_type, region,
                    estimated_monthly_savings, currency, confidence, implementation_effort,
                    risk, cost_basis, rollback_possible, detail, evidence_json,
                    remediation_json, tags_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        scan.scan_id,
                        f.id,
                        f.rule_id,
                        f.title,
                        f.category,
                        f.action_type,
                        f.service,
                        f.source,
                        f.resource_arn,
                        f.resource_id,
                        f.resource_type,
                        f.region,
                        f.estimated_monthly_savings,
                        f.currency,
                        f.confidence,
                        f.implementation_effort,
                        f.risk,
                        f.cost_basis,
                        int(f.rollback_possible),
                        f.detail,
                        _dumps([e.model_dump(mode="json") for e in f.evidence]),
                        f.remediation.model_dump_json() if f.remediation else None,
                        _dumps(f.tags),
                    )
                    for f in scan.findings
                ],
            )
            conn.executemany(
                """
                INSERT INTO costs (
                    scan_id, period_start, period_end, granularity, metric, amount, unit,
                    dim_service, dim_region, dim_usage_type, dimensions_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        scan.scan_id,
                        c.period_start.isoformat(),
                        c.period_end.isoformat(),
                        c.granularity,
                        c.metric,
                        c.amount,
                        c.unit,
                        c.dimensions.get("SERVICE"),
                        c.dimensions.get("REGION"),
                        c.dimensions.get("USAGE_TYPE"),
                        _dumps(c.dimensions),
                    )
                    for c in scan.costs
                ],
            )

    def save_advice(self, scan_id: str, advice: Advice) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE scans SET advice_json = ? WHERE scan_id = ?",
                (advice.model_dump_json(), scan_id),
            )

    def delete_scan(self, scan_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM scans WHERE scan_id = ?", (scan_id,))

    def delete_scans(self, scan_ids: Sequence[str]) -> int:
        """Delete the named scans. Returns how many rows actually went away."""
        if not scan_ids:
            return 0
        with self._connect() as conn:
            cursor = conn.executemany(
                "DELETE FROM scans WHERE scan_id = ?", [(scan_id,) for scan_id in scan_ids]
            )
            return cursor.rowcount if cursor.rowcount > 0 else 0

    def prune(self, keep: int) -> int:
        """Drop all but the ``keep`` most recent scans. Returns the number removed."""
        with self._connect() as conn:
            stale = [
                row["scan_id"]
                for row in conn.execute(
                    "SELECT scan_id FROM scans ORDER BY started_at DESC LIMIT -1 OFFSET ?",
                    (keep,),
                )
            ]
            for scan_id in stale:
                conn.execute("DELETE FROM scans WHERE scan_id = ?", (scan_id,))
            return len(stale)

    # ------------------------------------------------------------------- reads

    def list_scans(self, limit: int = 50) -> list[ScanMeta]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM scans ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_meta(row) for row in rows]

    def latest_scan_id(self) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT scan_id FROM scans ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        return row["scan_id"] if row else None

    def resolve_scan_id(self, scan_id: str | None) -> str | None:
        """Map ``None`` or ``"latest"`` to the most recent scan id."""
        if scan_id in (None, "", "latest"):
            return self.latest_scan_id()
        return scan_id

    def get_scan_meta(self, scan_id: str) -> ScanMeta | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM scans WHERE scan_id = ?", (scan_id,)).fetchone()
        return _row_to_meta(row) if row else None

    def get_scan(self, scan_id: str) -> Scan | None:
        """Hydrate a full scan, including every resource, finding, and cost record."""
        with self._connect() as conn:
            head = conn.execute("SELECT * FROM scans WHERE scan_id = ?", (scan_id,)).fetchone()
            if head is None:
                return None
            resources = [
                _row_to_resource(r)
                for r in conn.execute("SELECT * FROM resources WHERE scan_id = ?", (scan_id,))
            ]
            findings = [
                _row_to_finding(r)
                for r in conn.execute("SELECT * FROM findings WHERE scan_id = ?", (scan_id,))
            ]
            costs = [
                _row_to_cost(r)
                for r in conn.execute("SELECT * FROM costs WHERE scan_id = ?", (scan_id,))
            ]
        return Scan(
            scan_id=head["scan_id"],
            account_id=head["account_id"],
            account_alias=head["account_alias"],
            started_at=datetime.fromisoformat(head["started_at"]),
            finished_at=(
                datetime.fromisoformat(head["finished_at"]) if head["finished_at"] else None
            ),
            duration_seconds=head["duration_seconds"],
            regions=json.loads(head["regions_json"]),
            resources=resources,
            findings=findings,
            costs=costs,
            tco=TcoReport.model_validate_json(head["tco_json"]) if head["tco_json"] else None,
            advice=Advice.model_validate_json(head["advice_json"]) if head["advice_json"] else None,
            notes=[
                CapabilityNote.model_validate(n) for n in json.loads(head["notes_json"] or "[]")
            ],
            dry_run=bool(head["dry_run"]),
        )

    def get_tco(self, scan_id: str) -> TcoReport | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT tco_json FROM scans WHERE scan_id = ?", (scan_id,)
            ).fetchone()
        if row is None or not row["tco_json"]:
            return None
        return TcoReport.model_validate_json(row["tco_json"])

    def get_advice(self, scan_id: str) -> Advice | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT advice_json FROM scans WHERE scan_id = ?", (scan_id,)
            ).fetchone()
        if row is None or not row["advice_json"]:
            return None
        return Advice.model_validate_json(row["advice_json"])

    def get_notes(self, scan_id: str) -> list[CapabilityNote]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT notes_json FROM scans WHERE scan_id = ?", (scan_id,)
            ).fetchone()
        if row is None:
            return []
        return [CapabilityNote.model_validate(n) for n in json.loads(row["notes_json"] or "[]")]

    def query_resources(
        self,
        scan_id: str,
        *,
        service: str | None = None,
        region: str | None = None,
        resource_type: str | None = None,
        state: str | None = None,
        search: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> tuple[list[Resource], int]:
        where, params = ["scan_id = ?"], [scan_id]
        if service:
            where.append("service = ?")
            params.append(service)
        if region:
            where.append("region = ?")
            params.append(region)
        if resource_type:
            where.append("resource_type = ?")
            params.append(resource_type)
        if state:
            where.append("state = ?")
            params.append(state)
        if search:
            where.append("(resource_id LIKE ? OR name LIKE ? OR tags_json LIKE ?)")
            needle = f"%{search}%"
            params.extend([needle, needle, needle])
        clause = " AND ".join(where)
        with self._connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) AS n FROM resources WHERE {clause}", params
            ).fetchone()["n"]
            rows = conn.execute(
                f"SELECT * FROM resources WHERE {clause} "
                "ORDER BY COALESCE(monthly_cost, -1) DESC, resource_id LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return [_row_to_resource(r) for r in rows], total

    def query_findings(
        self,
        scan_id: str,
        *,
        category: str | None = None,
        service: str | None = None,
        region: str | None = None,
        source: str | None = None,
        effort: str | None = None,
        min_savings: float | None = None,
        search: str | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> tuple[list[Finding], int]:
        where, params = ["scan_id = ?"], [scan_id]
        for column, value in (
            ("category", category),
            ("service", service),
            ("region", region),
            ("source", source),
            ("implementation_effort", effort),
        ):
            if value:
                where.append(f"{column} = ?")
                params.append(value)
        if min_savings is not None:
            where.append("estimated_monthly_savings >= ?")
            params.append(min_savings)
        if search:
            where.append("(title LIKE ? OR resource_id LIKE ? OR detail LIKE ?)")
            needle = f"%{search}%"
            params.extend([needle, needle, needle])
        clause = " AND ".join(where)
        with self._connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) AS n FROM findings WHERE {clause}", params
            ).fetchone()["n"]
            rows = conn.execute(
                f"SELECT * FROM findings WHERE {clause} "
                "ORDER BY estimated_monthly_savings DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return [_row_to_finding(r) for r in rows], total

    def get_costs(
        self,
        scan_id: str,
        *,
        granularity: str | None = None,
        group_by: str | None = None,
    ) -> list[CostRecord]:
        where, params = ["scan_id = ?"], [scan_id]
        if granularity:
            where.append("granularity = ?")
            params.append(granularity)
        if group_by == "SERVICE":
            where.append("dim_service IS NOT NULL")
        elif group_by == "REGION":
            where.append("dim_region IS NOT NULL")
        elif group_by == "USAGE_TYPE":
            where.append("dim_usage_type IS NOT NULL")
        clause = " AND ".join(where)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM costs WHERE {clause} ORDER BY period_start", params
            ).fetchall()
        return [_row_to_cost(r) for r in rows]

    def trend(self, limit: int = 30) -> list[ScanMeta]:
        """Oldest-first scan history for the trend chart."""
        return list(reversed(self.list_scans(limit=limit)))

    def distinct_values(self, scan_id: str, table: str, column: str) -> list[str]:
        if table not in {"resources", "findings"}:
            raise ValueError(f"Unsupported table: {table}")
        allowed = {
            "resources": {"service", "region", "resource_type", "state"},
            "findings": {"category", "service", "region", "source", "implementation_effort"},
        }
        if column not in allowed[table]:
            raise ValueError(f"Unsupported column: {table}.{column}")
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT DISTINCT {column} AS v FROM {table} "
                f"WHERE scan_id = ? AND {column} IS NOT NULL ORDER BY v",
                (scan_id,),
            ).fetchall()
        return [row["v"] for row in rows]


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an existing database up to the current schema.

    Additive only. The store is a history of past runs: worth keeping, never worth
    rebuilding from scratch because a column was added.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(scans)")}
    if "dry_run" not in columns:
        conn.execute("ALTER TABLE scans ADD COLUMN dry_run INTEGER NOT NULL DEFAULT 0")
        # Demo scans written before the column existed are recognizable by the alias the
        # mocked account carries.
        conn.execute("UPDATE scans SET dry_run = 1 WHERE account_alias LIKE '%dry run%'")


def _row_to_meta(row: sqlite3.Row) -> ScanMeta:
    return ScanMeta(
        scan_id=row["scan_id"],
        account_id=row["account_id"],
        account_alias=row["account_alias"],
        started_at=datetime.fromisoformat(row["started_at"]),
        finished_at=datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
        duration_seconds=row["duration_seconds"],
        regions=json.loads(row["regions_json"]),
        resource_count=row["resource_count"],
        finding_count=row["finding_count"],
        monthly_run_rate=row["monthly_run_rate"],
        identified_monthly_savings=row["identified_monthly_savings"],
        dry_run=bool(row["dry_run"]),
    )


def _row_to_resource(row: sqlite3.Row) -> Resource:
    return Resource(
        arn=row["arn"],
        resource_id=row["resource_id"],
        resource_type=row["resource_type"],
        service=row["service"],
        region=row["region"],
        account_id=row["account_id"],
        name=row["name"],
        availability_zone=row["availability_zone"],
        state=row["state"],
        created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
        tags=json.loads(row["tags_json"]),
        attributes=json.loads(row["attributes_json"]),
        metrics=json.loads(row["metrics_json"]),
        monthly_cost=row["monthly_cost"],
        cost_basis=row["cost_basis"],
    )


def _row_to_finding(row: sqlite3.Row) -> Finding:
    return Finding(
        id=row["finding_id"],
        rule_id=row["rule_id"],
        title=row["title"],
        category=row["category"],
        action_type=row["action_type"],
        service=row["service"],
        source=row["source"],
        resource_arn=row["resource_arn"],
        resource_id=row["resource_id"],
        resource_type=row["resource_type"],
        region=row["region"],
        estimated_monthly_savings=row["estimated_monthly_savings"],
        currency=row["currency"],
        confidence=row["confidence"],
        implementation_effort=row["implementation_effort"],
        risk=row["risk"],
        cost_basis=row["cost_basis"],
        rollback_possible=bool(row["rollback_possible"]),
        detail=row["detail"],
        evidence=json.loads(row["evidence_json"]),
        remediation=(json.loads(row["remediation_json"]) if row["remediation_json"] else None),
        tags=json.loads(row["tags_json"]),
    )


def _row_to_cost(row: sqlite3.Row) -> CostRecord:
    return CostRecord(
        period_start=row["period_start"],
        period_end=row["period_end"],
        granularity=row["granularity"],
        metric=row["metric"],
        amount=row["amount"],
        unit=row["unit"],
        dimensions=json.loads(row["dimensions_json"]),
    )


def open_store(db_path: str | Path | None = None) -> ScanStore:
    from finops.config import get_settings

    return ScanStore(db_path or get_settings().db_path)


def summarize_costs(records: Sequence[CostRecord], dimension: str) -> dict[str, float]:
    """Total cost per value of ``dimension`` across the given records."""
    totals: dict[str, float] = {}
    for record in records:
        key = record.dimensions.get(dimension)
        if key is None:
            continue
        totals[key] = totals.get(key, 0.0) + record.amount
    return totals
