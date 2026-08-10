"""FastAPI surface consumed by the dashboard.

Every read is served from SQLite, so the UI is fast and stays usable while a scan runs.
The only write is "start a scan", which returns immediately with a job handle.

Scan ids accept the literal ``latest`` anywhere a real id is expected, which keeps the
frontend from having to resolve it first.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from finops import __version__
from finops.aws.session import AwsContext
from finops.config import Settings, get_settings
from finops.jobs import ScanAlreadyRunning, ScanRunner
from finops.model import (
    Advice,
    CapabilityNote,
    CostRecord,
    Finding,
    Resource,
    ScanMeta,
    TcoReport,
)
from finops.pipeline import ScanOptions, regenerate_advice
from finops.store import ScanStore
from finops.tco import compare_scans

logger = logging.getLogger(__name__)

DEV_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:4173",
]


class Page(BaseModel):
    """A slice of a larger result set."""

    total: int
    limit: int
    offset: int


class ResourcePage(Page):
    items: list[Resource]


class FindingPage(Page):
    items: list[Finding]


class ScanDetail(BaseModel):
    meta: ScanMeta
    tco: TcoReport | None = None
    notes: list[CapabilityNote] = Field(default_factory=list)


class StartScanRequest(BaseModel):
    regions: list[str] | None = None
    collectors: list[str] | None = None
    skip_collectors: list[str] = Field(default_factory=list)
    with_metrics: bool = True
    with_native: bool = True
    with_advice: bool = True

    def to_options(self) -> ScanOptions:
        return ScanOptions(
            regions=self.regions,
            collectors=self.collectors,
            skip_collectors=self.skip_collectors,
            with_metrics=self.with_metrics,
            with_native=self.with_native,
            with_advice=self.with_advice,
        )


class FilterOptions(BaseModel):
    """Distinct values for the UI's filter dropdowns, so it never hardcodes them."""

    services: list[str] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)
    resource_types: list[str] = Field(default_factory=list)
    states: list[str] = Field(default_factory=list)
    finding_categories: list[str] = Field(default_factory=list)
    finding_sources: list[str] = Field(default_factory=list)
    efforts: list[str] = Field(default_factory=list)


def get_store(request: Request) -> ScanStore:
    return request.app.state.store


def get_runner(request: Request) -> ScanRunner:
    return request.app.state.runner


def app_settings(request: Request) -> Settings:
    return request.app.state.settings


def resolve(store: ScanStore, scan_id: str) -> str:
    resolved = store.resolve_scan_id(scan_id)
    if resolved is None:
        raise HTTPException(
            status_code=404,
            detail="No scans yet. Run `finops scan` or POST /api/scans to create one.",
        )
    if store.get_scan_meta(resolved) is None:
        raise HTTPException(status_code=404, detail=f"Unknown scan: {scan_id}")
    return resolved


router = APIRouter(prefix="/api")


@router.get("/health")
def health(
    store: ScanStore = Depends(get_store),
    settings: Settings = Depends(app_settings),
) -> dict[str, Any]:
    return {
        "status": "ok",
        "version": __version__,
        "database": str(store.db_path),
        "latest_scan_id": store.latest_scan_id(),
        "llm_provider": settings.llm_provider,
    }


@router.get("/identity")
def identity(settings: Settings = Depends(app_settings)) -> dict[str, Any]:
    """Who the agent is authenticated as. Slow path, so it is not part of /health."""
    try:
        aws = AwsContext(settings=settings)
        return {
            "account_id": aws.account_id,
            "account_alias": aws.account_alias,
            "regions": aws.regions,
            "profile": settings.aws_profile,
        }
    except Exception as exc:  # noqa: BLE001 - missing credentials is a normal state here
        raise HTTPException(status_code=503, detail=f"AWS credentials unavailable: {exc}") from exc


# ------------------------------------------------------------------ scan lifecycle


@router.get("/scans", response_model=list[ScanMeta])
def list_scans(limit: int = Query(50, ge=1, le=500), store: ScanStore = Depends(get_store)):
    return store.list_scans(limit=limit)


@router.post("/scans", status_code=202)
def start_scan(
    body: StartScanRequest | None = None,
    runner: ScanRunner = Depends(get_runner),
) -> dict[str, Any]:
    try:
        return runner.start((body or StartScanRequest()).to_options())
    except ScanAlreadyRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/scans/status")
def scan_status(runner: ScanRunner = Depends(get_runner)) -> dict[str, Any]:
    """Progress of the running scan, or the outcome of the last one."""
    return runner.status() or {"status": "idle", "stage": None, "message": None, "log": []}


@router.get("/scans/{scan_id}", response_model=ScanDetail)
def get_scan(scan_id: str, store: ScanStore = Depends(get_store)):
    resolved = resolve(store, scan_id)
    meta = store.get_scan_meta(resolved)
    assert meta is not None  # resolve() already proved it exists
    return ScanDetail(meta=meta, tco=store.get_tco(resolved), notes=store.get_notes(resolved))


@router.delete("/scans/{scan_id}", status_code=204)
def delete_scan(scan_id: str, store: ScanStore = Depends(get_store)) -> None:
    store.delete_scan(resolve(store, scan_id))


# ------------------------------------------------------------------------ reports


@router.get("/scans/{scan_id}/tco", response_model=TcoReport)
def get_tco(scan_id: str, store: ScanStore = Depends(get_store)):
    report = store.get_tco(resolve(store, scan_id))
    if report is None:
        raise HTTPException(status_code=404, detail="This scan has no TCO report")
    return report


@router.get("/scans/{scan_id}/costs", response_model=list[CostRecord])
def get_costs(
    scan_id: str,
    granularity: Literal["HOURLY", "DAILY", "MONTHLY"] | None = None,
    group_by: Literal["SERVICE", "REGION", "USAGE_TYPE"] | None = None,
    store: ScanStore = Depends(get_store),
):
    return store.get_costs(resolve(store, scan_id), granularity=granularity, group_by=group_by)


@router.get("/scans/{scan_id}/resources", response_model=ResourcePage)
def get_resources(
    scan_id: str,
    service: str | None = None,
    region: str | None = None,
    resource_type: str | None = None,
    state: str | None = None,
    search: str | None = None,
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    store: ScanStore = Depends(get_store),
):
    items, total = store.query_resources(
        resolve(store, scan_id),
        service=service,
        region=region,
        resource_type=resource_type,
        state=state,
        search=search,
        limit=limit,
        offset=offset,
    )
    return ResourcePage(items=items, total=total, limit=limit, offset=offset)


@router.get("/scans/{scan_id}/resource", response_model=Resource)
def get_resource(
    scan_id: str,
    arn: str = Query(..., description="Full resource ARN"),
    store: ScanStore = Depends(get_store),
):
    """Single resource by ARN. A query parameter, because ARNs contain slashes."""
    items, _ = store.query_resources(resolve(store, scan_id), search=None, limit=2000)
    for resource in items:
        if resource.arn == arn:
            return resource
    raise HTTPException(status_code=404, detail=f"Resource not in this scan: {arn}")


@router.get("/scans/{scan_id}/findings", response_model=FindingPage)
def get_findings(
    scan_id: str,
    category: str | None = None,
    service: str | None = None,
    region: str | None = None,
    source: str | None = None,
    effort: str | None = None,
    min_savings: float | None = None,
    search: str | None = None,
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    store: ScanStore = Depends(get_store),
):
    items, total = store.query_findings(
        resolve(store, scan_id),
        category=category,
        service=service,
        region=region,
        source=source,
        effort=effort,
        min_savings=min_savings,
        search=search,
        limit=limit,
        offset=offset,
    )
    return FindingPage(items=items, total=total, limit=limit, offset=offset)


@router.get("/scans/{scan_id}/filters", response_model=FilterOptions)
def get_filters(scan_id: str, store: ScanStore = Depends(get_store)):
    resolved = resolve(store, scan_id)
    return FilterOptions(
        services=store.distinct_values(resolved, "resources", "service"),
        regions=store.distinct_values(resolved, "resources", "region"),
        resource_types=store.distinct_values(resolved, "resources", "resource_type"),
        states=store.distinct_values(resolved, "resources", "state"),
        finding_categories=store.distinct_values(resolved, "findings", "category"),
        finding_sources=store.distinct_values(resolved, "findings", "source"),
        efforts=store.distinct_values(resolved, "findings", "implementation_effort"),
    )


@router.get("/scans/{scan_id}/notes", response_model=list[CapabilityNote])
def get_notes(scan_id: str, store: ScanStore = Depends(get_store)):
    """Data sources the scan could not reach, so the UI can explain the gaps."""
    return store.get_notes(resolve(store, scan_id))


# ------------------------------------------------------------------------- advice


@router.get("/scans/{scan_id}/advice", response_model=Advice)
def get_advice(scan_id: str, store: ScanStore = Depends(get_store)):
    advice = store.get_advice(resolve(store, scan_id))
    if advice is None:
        raise HTTPException(
            status_code=404,
            detail="No advice for this scan. POST to this path to generate it.",
        )
    return advice


@router.post("/scans/{scan_id}/advice", response_model=Advice)
def create_advice(
    scan_id: str,
    store: ScanStore = Depends(get_store),
    settings: Settings = Depends(app_settings),
):
    """Re-run only the LLM layer. Cheap: no Cost Explorer calls."""
    resolved = resolve(store, scan_id)
    scan = store.get_scan(resolved)
    if scan is None or scan.tco is None:
        raise HTTPException(status_code=404, detail="Scan has no report to advise on")
    updated = regenerate_advice(scan, settings, store=store)
    assert updated.advice is not None
    return updated.advice


# ------------------------------------------------------------------------- trends


@router.get("/trends", response_model=list[ScanMeta])
def trends(limit: int = Query(30, ge=2, le=200), store: ScanStore = Depends(get_store)):
    """Oldest-first scan history, so the chart reads left to right."""
    return store.trend(limit=limit)


@router.get("/scans/{scan_id}/compare")
def compare(
    scan_id: str,
    against: str = Query("previous", description="A scan id, or 'previous'"),
    store: ScanStore = Depends(get_store),
) -> dict[str, Any]:
    resolved = resolve(store, scan_id)
    current = store.get_tco(resolved)
    if current is None:
        raise HTTPException(status_code=404, detail="This scan has no TCO report")

    if against == "previous":
        history = store.list_scans(limit=200)
        ids = [meta.scan_id for meta in history]
        try:
            baseline_id = ids[ids.index(resolved) + 1]
        except (ValueError, IndexError):
            baseline_id = None
    else:
        baseline_id = resolve(store, against)

    previous = store.get_tco(baseline_id) if baseline_id else None
    return {
        "scan_id": resolved,
        "baseline_scan_id": baseline_id if previous else None,
        **compare_scans(current, previous),
    }


def create_app(
    store: ScanStore | None = None,
    settings: Settings | None = None,
    *,
    runner: ScanRunner | None = None,
    static_dir: Path | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    store = store or ScanStore(settings.db_path)

    app = FastAPI(
        title="FinOps Agent",
        version=__version__,
        description=(
            "Read-only AWS cost analysis: inventory, TCO, savings, and architecture advice."
        ),
    )
    app.state.settings = settings
    app.state.store = store
    app.state.runner = runner or ScanRunner(store, settings)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=DEV_ORIGINS,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    _mount_frontend(app, static_dir)
    return app


def _mount_frontend(app: FastAPI, static_dir: Path | None) -> None:
    """Serve the built dashboard from the same origin when it has been built."""
    dist = static_dir or (Path(__file__).resolve().parents[2] / "frontend" / "dist")
    if not dist.is_dir():
        logger.info("No built frontend at %s; API only. Run `npm run dev` for the UI.", dist)
        return

    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        # Client-side routing: unknown paths return index.html, not a 404.
        candidate = dist / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(dist / "index.html")
