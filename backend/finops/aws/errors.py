"""Graceful degradation for partially-permitted accounts.

A FinOps scan touches dozens of APIs, and real accounts rarely allow all of them:
Compute Optimizer needs an opt-in, Trusted Advisor needs Business support, and a
read-only role may simply be missing an action. Rather than aborting, each data source
is wrapped in :func:`graceful`, which records why the data is missing so the dashboard
can tell the user what they are not seeing.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from botocore.exceptions import BotoCoreError, ClientError

from finops.model import CapabilityNote, CapabilityStatus

logger = logging.getLogger(__name__)

_DENIED_CODES = frozenset(
    {
        "AccessDenied",
        "AccessDeniedException",
        "UnauthorizedOperation",
        "AuthFailure",
        "AuthorizationError",
        "InvalidClientTokenId",
        "UnrecognizedClientException",
        "ExpiredToken",
        "ExpiredTokenException",
        "InvalidAccessKeyId",
        "SignatureDoesNotMatch",
        "Forbidden",
    }
)

_NOT_ENROLLED_CODES = frozenset(
    {
        "OptInRequired",
        "SubscriptionRequiredException",
        "OptInRequiredException",
    }
)

_UNAVAILABLE_CODES = frozenset(
    {
        "DataUnavailableException",
        "ResourceNotFoundException",
        "InvalidAction",
        "UnknownOperationException",
        "EndpointConnectionError",
        "InvalidRequestException",
        "BillExpirationException",
    }
)

_REMEDIES: dict[CapabilityStatus, str] = {
    "denied": "Attach the policy in iam/finops-readonly-policy.json to the identity you are using.",
    "not_enrolled": "Enable this service in the AWS console; enrollment is free but required.",
    "unavailable": "This data source is not available for this account or region.",
}


def classify_error_code(code: str) -> CapabilityStatus:
    if code in _DENIED_CODES:
        return "denied"
    if code in _NOT_ENROLLED_CODES:
        return "not_enrolled"
    if code in _UNAVAILABLE_CODES:
        return "unavailable"
    return "error"


def error_code(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code", "Unknown"))
    return type(exc).__name__


class NoteCollector:
    """Thread-safe accumulator for capability notes raised during a scan."""

    def __init__(self) -> None:
        self._notes: dict[tuple[str, str | None, str], CapabilityNote] = {}
        self._lock = threading.Lock()

    def add(
        self,
        capability: str,
        status: CapabilityStatus,
        message: str,
        *,
        region: str | None = None,
        remedy: str | None = None,
    ) -> None:
        note = CapabilityNote(
            capability=capability,
            status=status,
            message=message,
            region=region,
            remedy=remedy or _REMEDIES.get(status),
        )
        with self._lock:
            # Keyed so the same denial across 20 regions does not produce 20 identical notes.
            self._notes.setdefault((capability, region, status), note)

    def add_from_exception(
        self, capability: str, exc: Exception, *, region: str | None = None
    ) -> CapabilityNote:
        code = error_code(exc)
        status = classify_error_code(code)
        self.add(capability, status, f"{code}: {exc}", region=region)
        return self._notes[(capability, region, status)]

    @property
    def notes(self) -> list[CapabilityNote]:
        with self._lock:
            return sorted(
                self._notes.values(),
                key=lambda n: (n.status != "error", n.capability, n.region or ""),
            )

    def has_problem(self, capability: str) -> bool:
        with self._lock:
            return any(key[0] == capability for key in self._notes)


@contextmanager
def graceful(
    notes: NoteCollector,
    capability: str,
    *,
    region: str | None = None,
    reraise: bool = False,
) -> Iterator[None]:
    """Swallow AWS errors from one data source and record why it was skipped."""
    try:
        yield
    except (ClientError, BotoCoreError) as exc:
        note = notes.add_from_exception(capability, exc, region=region)
        log = logger.warning if note.status == "error" else logger.info
        log("Skipping %s%s: %s", capability, f" in {region}" if region else "", note.message)
        if reraise:
            raise
    except Exception as exc:  # noqa: BLE001 - a collector bug must not kill the scan
        notes.add(capability, "error", f"{type(exc).__name__}: {exc}", region=region)
        logger.exception(
            "Unexpected failure collecting %s%s", capability, f" in {region}" if region else ""
        )
        if reraise:
            raise
