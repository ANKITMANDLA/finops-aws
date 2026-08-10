"""Background scan execution.

A scan takes minutes and costs money in Cost Explorer requests, so the API never runs
one inline. It starts a single background job, streams progress into memory, and serves
every read from SQLite. The dashboard therefore stays responsive - and usable - while a
scan is in flight.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from finops.config import Settings, get_settings
from finops.model import utcnow
from finops.pipeline import ScanOptions, run_scan
from finops.store import ScanStore

logger = logging.getLogger(__name__)

JobStatus = Literal["queued", "running", "succeeded", "failed"]

MAX_LOG_LINES = 200


@dataclass
class ScanJob:
    job_id: str
    status: JobStatus = "queued"
    stage: str = "queued"
    message: str = "Waiting to start"
    started_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None
    scan_id: str | None = None
    error: str | None = None
    log: deque[str] = field(default_factory=lambda: deque(maxlen=MAX_LOG_LINES))

    def as_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "stage": self.stage,
            "message": self.message,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "scan_id": self.scan_id,
            "error": self.error,
            "log": list(self.log),
        }


class ScanAlreadyRunning(RuntimeError):
    """A scan is already in flight; starting a second one would double the API spend."""


class ScanRunner:
    """Runs at most one scan at a time and exposes its progress."""

    def __init__(self, store: ScanStore, settings: Settings | None = None) -> None:
        self.store = store
        self.settings = settings or get_settings()
        self._lock = threading.Lock()
        self._current: ScanJob | None = None
        self._last: ScanJob | None = None
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._current is not None

    def status(self) -> dict | None:
        with self._lock:
            job = self._current or self._last
        return job.as_dict() if job else None

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            for job in (self._current, self._last):
                if job and job.job_id == job_id:
                    return job.as_dict()
        return None

    def start(self, options: ScanOptions | None = None) -> dict:
        with self._lock:
            if self._current is not None:
                raise ScanAlreadyRunning(
                    f"Scan {self._current.job_id} is already running ({self._current.stage})"
                )
            job = ScanJob(job_id=uuid.uuid4().hex[:12])
            self._current = job
            thread = threading.Thread(
                target=self._run,
                args=(job, options or ScanOptions()),
                daemon=True,
                name=f"scan-{job.job_id}",
            )
            self._thread = thread
            # Snapshot before starting so the caller always sees the queued state rather
            # than whatever the worker has already raced ahead to.
            handle = job.as_dict()
        thread.start()
        return handle

    def _run(self, job: ScanJob, options: ScanOptions) -> None:
        job.status = "running"

        def progress(stage: str, message: str) -> None:
            job.stage = stage
            job.message = message
            job.log.append(f"{stage}: {message}")

        try:
            scan = run_scan(self.settings, options, store=self.store, progress=progress)
            job.scan_id = scan.scan_id
            job.status = "succeeded"
            job.stage = "done"
            job.message = (
                f"{len(scan.resources)} resources, {len(scan.findings)} findings, "
                f"${scan.tco.identified_monthly_savings:,.0f}/mo identified"
                if scan.tco
                else "Scan complete"
            )
        except Exception as exc:  # noqa: BLE001 - the job must record why it died
            logger.exception("Scan job %s failed", job.job_id)
            job.status = "failed"
            job.stage = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            job.message = job.error
        finally:
            job.finished_at = utcnow()
            with self._lock:
                self._last = job
                self._current = None

    def wait(self, timeout: float | None = None) -> None:
        """Block until the running scan finishes. Used by the CLI and by tests."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
