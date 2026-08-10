"""Collector contract, registry, and the parallel inventory runner."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

from botocore.client import BaseClient

from finops.aws.errors import NoteCollector, graceful
from finops.aws.session import AwsContext
from finops.config import Settings
from finops.model import Resource

logger = logging.getLogger(__name__)

Scope = Literal["regional", "global"]


@dataclass
class CollectionContext:
    """Everything a collector needs: credentials, config, and a place to log gaps."""

    aws: AwsContext
    notes: NoteCollector = field(default_factory=NoteCollector)
    # Regions this scan covers. Global collectors use it to filter their results so a
    # region-scoped scan does not report buckets from regions the user excluded.
    target_regions: list[str] = field(default_factory=list)

    @property
    def settings(self) -> Settings:
        return self.aws.settings

    def in_scope(self, region: str | None) -> bool:
        if not self.target_regions or region is None:
            return True
        return region in self.target_regions

    @property
    def account_id(self) -> str:
        return self.aws.account_id

    def client(self, service: str, region: str | None = None) -> BaseClient:
        return self.aws.client(service, region)


class Collector(ABC):
    """Collects one service's resources in one region."""

    key: ClassVar[str]
    service: ClassVar[str]
    scope: ClassVar[Scope] = "regional"

    @abstractmethod
    def collect(self, ctx: CollectionContext, region: str) -> list[Resource]:
        """Return the resources found. Raising is fine; the runner records the failure."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} key={self.key}>"


REGISTRY: dict[str, type[Collector]] = {}


def register(cls: type[Collector]) -> type[Collector]:
    if not getattr(cls, "key", None):
        raise ValueError(f"{cls.__name__} must define a 'key'")
    if cls.key in REGISTRY:
        raise ValueError(f"Duplicate collector key: {cls.key}")
    REGISTRY[cls.key] = cls
    return cls


def build_collectors(
    only: Sequence[str] | None = None, skip: Sequence[str] | None = None
) -> list[Collector]:
    keys = list(only) if only else sorted(REGISTRY)
    unknown = [k for k in keys if k not in REGISTRY]
    if unknown:
        raise ValueError(
            f"Unknown collector(s): {', '.join(unknown)}. Available: {', '.join(sorted(REGISTRY))}"
        )
    if skip:
        keys = [k for k in keys if k not in set(skip)]
    return [REGISTRY[k]() for k in keys]


ProgressCallback = Callable[[str, str, int], None]


def collect_inventory(
    ctx: CollectionContext,
    *,
    only: Sequence[str] | None = None,
    skip: Sequence[str] | None = None,
    regions: Sequence[str] | None = None,
    progress: ProgressCallback | None = None,
) -> list[Resource]:
    """Run every collector across every region in parallel.

    Failures are isolated per (collector, region) so one denied API or one bad region
    cannot empty the whole inventory.
    """
    collectors = build_collectors(only, skip)
    target_regions = list(regions) if regions else ctx.aws.regions
    ctx.target_regions = target_regions
    resources: list[Resource] = []

    tasks: list[tuple[Collector, str]] = []
    for collector in collectors:
        if collector.scope == "global":
            tasks.append((collector, ctx.aws.default_region))
        else:
            tasks.extend((collector, region) for region in target_regions)

    def run(collector: Collector, region: str) -> list[Resource]:
        found: list[Resource] = []
        with graceful(ctx.notes, collector.key, region=region):
            found = collector.collect(ctx, region)
        if progress:
            progress(collector.key, region, len(found))
        return found

    max_workers = min(ctx.settings.max_workers, max(len(tasks), 1))
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="collect") as pool:
        futures = {
            pool.submit(run, collector, region): (collector, region) for collector, region in tasks
        }
        for future in as_completed(futures):
            collector, region = futures[future]
            try:
                resources.extend(future.result())
            except Exception:  # noqa: BLE001 - graceful() already recorded a note
                logger.debug("Collector %s failed in %s", collector.key, region, exc_info=True)

    logger.info(
        "Collected %d resources from %d collectors across %d regions",
        len(resources),
        len(collectors),
        len(target_regions),
    )
    return resources


# --------------------------------------------------------------------- utilities


def tags_to_dict(
    items: Sequence[dict[str, Any]] | None, key: str = "Key", value: str = "Value"
) -> dict[str, str]:
    """Normalize AWS's many tag shapes into a plain dict."""
    if not items:
        return {}
    return {str(item[key]): str(item.get(value, "")) for item in items if key in item}


def paginate(
    client: BaseClient, operation: str, result_key: str, **kwargs: Any
) -> Iterator[dict[str, Any]]:
    """Yield every item under ``result_key``, paginating when the API supports it."""
    if client.can_paginate(operation):
        for page in client.get_paginator(operation).paginate(**kwargs):
            yield from page.get(result_key, []) or []
    else:
        response = getattr(client, operation)(**kwargs)
        yield from response.get(result_key, []) or []


def synthesize_arn(
    service: str, region: str, account_id: str, resource_path: str, *, partition: str = "aws"
) -> str:
    """Build an ARN for services whose describe calls do not return one."""
    return f"arn:{partition}:{service}:{region}:{account_id}:{resource_path}"


def az_to_region(availability_zone: str | None) -> str | None:
    return availability_zone[:-1] if availability_zone else None
