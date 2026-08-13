"""List prices read from the price list files AWS publishes for anyone to download.

``pricing:GetProducts`` is the tidy way to get a rate, but plenty of read-only roles are
not granted it, and asking for it can take longer than the analysis itself. AWS also
publishes the same data as static files on an unauthenticated endpoint, so a locked-down
identity can still price its own resources:

    https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/us-west-2/index.json

Same rates, same product attributes, same usage types, so the filters in
:mod:`finops.aws.pricing` apply unchanged and nothing here invents a number.

The files are big — EC2 is around 450MB per region, most of which is reserved instance
terms we never read — so each one is streamed once, reduced to the handful of on-demand
charges a scan asks about, and cached on disk. Later scans read the cache.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PRICE_LIST_BASE = "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws"

# Bumped whenever what we keep from a file changes, so old caches are refetched rather
# than read with fields missing.
CACHE_VERSION = 2

# Attributes worth keeping: everything the pricing lookups filter or select on.
KEPT_ATTRIBUTES = frozenset(
    {
        "usagetype",
        "operation",
        "group",
        "productFamily",
        "regionCode",
        "instanceType",
        "operatingSystem",
        "tenancy",
        "preInstalledSw",
        "capacitystatus",
        "marketoption",
        "licenseModel",
        "volumeApiName",
        "volumeType",
        "storageClass",
        "databaseEngine",
        "deploymentOption",
        "tiertype",
    }
)

# EC2's file is two orders of magnitude larger than the rest because it lists every
# instance permutation. We only ever price shared-tenancy on-demand instances, so
# discarding the rest keeps the cache small enough to load instantly.
_ON_DEMAND_INSTANCE_SHAPE = {
    "tenancy": "Shared",
    "preInstalledSw": "NA",
    "capacitystatus": "Used",
    "marketoption": "OnDemand",
}

_BLOCK_START = re.compile(r'^\s+"([^"]+)"\s*:\s*\{\s*$')

# Keys that hold other objects rather than being data themselves. Everything after
# terms.OnDemand is reserved instance and savings plan pricing, which is most of the file
# and none of our business.
_SECTIONS = {"products": "products", "OnDemand": "ondemand", "terms": None}
_STOP_AT = {"Reserved", "savingsPlan"}

# A data block is a few dozen lines. Anything longer means the layout is not what we
# expect, and reading on would just buffer the rest of the file.
_MAX_BLOCK_LINES = 10_000


def matches_filters(attributes: dict[str, Any], filters: dict[str, str]) -> bool:
    """Apply the Price List API's TERM_MATCH semantics: equality, one attribute at a time."""
    return all(attributes.get(field) == value for field, value in filters.items())


def usage_type_matches(attributes: dict[str, Any], pattern: re.Pattern[str]) -> bool:
    """Match a usage type while ignoring the region prefix AWS puts in front of it.

    Usage types look like ``USW2-EBS:SnapshotUsage`` everywhere except us-east-1, where
    the prefix is absent. The prefix is an opaque code with no mapping from region names,
    so compare against both the whole string and everything after the first dash.
    """
    usage = attributes.get("usagetype") or ""
    if pattern.fullmatch(usage):
        return True
    tail = usage.partition("-")[2]
    return bool(tail and pattern.fullmatch(tail))


@dataclass(frozen=True)
class PublishedRate:
    """One on-demand charge as published, reduced to what a lookup needs."""

    attributes: dict[str, str]
    amount: float
    unit: str = ""


class PriceListUnavailable(RuntimeError):
    """The published file could not be fetched or made sense of."""


class PublicPriceList:
    """On-demand rates from the published price list files, cached per service and region."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        timeout: float = 600.0,
        base_url: str = PRICE_LIST_BASE,
        opener=None,
    ) -> None:
        self.cache_dir = cache_dir
        self.timeout = timeout
        self.base_url = base_url
        self._opener = opener or self._open_url
        self._catalogs: dict[tuple[str, str], list[PublishedRate]] = {}
        self._failed: set[tuple[str, str]] = set()
        self._lock = threading.Lock()

    @property
    def downloaded(self) -> list[str]:
        """Which published files this run has read, for reporting."""
        return sorted(f"{service} {region}" for service, region in self._catalogs)

    def rate(
        self, service_code: str, region: str, filters: dict[str, str], usage_type: str | None
    ) -> tuple[float, str] | None:
        """The first paid rate matching these filters, with the unit AWS quoted it in."""
        try:
            catalog = self._catalog(service_code, region)
        except PriceListUnavailable as exc:
            logger.warning(
                "Public price list unavailable for %s in %s: %s", service_code, region, exc
            )
            return None

        pattern = re.compile(usage_type) if usage_type else None
        for published in catalog:
            if not matches_filters(published.attributes, filters):
                continue
            if pattern and not usage_type_matches(published.attributes, pattern):
                continue
            return published.amount, published.unit
        return None

    # ------------------------------------------------------------------ catalogs

    def _catalog(self, service_code: str, region: str) -> list[PublishedRate]:
        key = (service_code, region)
        with self._lock:
            if key in self._catalogs:
                return self._catalogs[key]
            if key in self._failed:
                raise PriceListUnavailable("already failed once this run")

        catalog = self._load_cached(service_code, region)
        if catalog is None:
            try:
                catalog = self._download(service_code, region)
            except PriceListUnavailable:
                with self._lock:
                    self._failed.add(key)
                raise
            self._save_cached(service_code, region, catalog)

        with self._lock:
            self._catalogs[key] = catalog
        return catalog

    def _cache_file(self, service_code: str, region: str) -> Path:
        return self.cache_dir / f"{service_code}-{region}.json"

    def _load_cached(self, service_code: str, region: str) -> list[PublishedRate] | None:
        path = self._cache_file(service_code, region)
        try:
            if not path.exists():
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("version") != CACHE_VERSION:
                return None
            return [
                PublishedRate(
                    attributes=item["attributes"],
                    amount=item["amount"],
                    unit=item.get("unit", ""),
                )
                for item in payload["rates"]
            ]
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.debug("Ignoring unreadable price list cache %s: %s", path, exc)
            return None

    def _save_cached(self, service_code: str, region: str, catalog: list[PublishedRate]) -> None:
        path = self._cache_file(service_code, region)
        payload = {
            "version": CACHE_VERSION,
            "service": service_code,
            "region": region,
            "rates": [
                {"attributes": rate.attributes, "amount": rate.amount, "unit": rate.unit}
                for rate in catalog
            ],
        }
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError as exc:
            logger.debug("Could not write price list cache %s: %s", path, exc)

    def _download(self, service_code: str, region: str) -> list[PublishedRate]:
        url = f"{self.base_url}/{service_code}/current/{region}/index.json"
        logger.info(
            "Reading AWS's published price list for %s in %s, once per region. Caching in %s.",
            service_code,
            region,
            self.cache_dir,
        )
        catalog = extract_rates(self._opener(url), keep=_keeper(service_code))
        if not catalog:
            raise PriceListUnavailable(f"no on-demand rates found in {url}")
        logger.info("Cached %d published rates for %s in %s", len(catalog), service_code, region)
        return catalog

    def _open_url(self, url: str) -> Iterator[bytes]:
        request = urllib.request.Request(url, headers={"Accept-Encoding": "identity"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                yield from response
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise PriceListUnavailable(str(exc)) from exc


def _keeper(service_code: str):
    """Which products are worth caching for this service."""
    if service_code != "AmazonEC2":
        return None

    def keep(product: dict[str, Any]) -> bool:
        attributes = product.get("attributes") or {}
        if product.get("productFamily") != "Compute Instance":
            return True
        return all(
            attributes.get(field) == value for field, value in _ON_DEMAND_INSTANCE_SHAPE.items()
        )

    return keep


def extract_rates(lines, keep=None) -> list[PublishedRate]:
    """Reduce a streamed price list file to its on-demand rates.

    The files are machine-generated with one key per line, so they can be read as a
    sequence of indented blocks without holding the whole document in memory. Reading
    stops at the reserved instance terms, which are the bulk of the bytes and of no
    interest here.
    """
    products: dict[str, dict[str, str]] = {}
    rates: list[PublishedRate] = []
    section: str | None = None
    key = ""
    buffer: list[str] = []
    depth = 0

    for raw in lines:
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw

        if depth == 0:
            match = _BLOCK_START.match(line)
            if match is None:
                continue
            name = match.group(1)
            if name in _STOP_AT:
                break
            if name in _SECTIONS:
                section = _SECTIONS[name]
                continue
            if section is None:
                continue
            key = name
            buffer = ["{"]
            depth = 1
            continue

        buffer.append(line)
        depth += line.count("{") - line.count("}")
        if depth > 0:
            if len(buffer) <= _MAX_BLOCK_LINES:
                continue
            raise PriceListUnavailable("price list layout is not the one this reader expects")

        block = _parse_block(buffer)
        buffer = []
        if block is None:
            continue
        if section == "products":
            attributes = _reduce_attributes(block)
            if keep is None or keep(block):
                products[key] = attributes
        elif key in products:
            paid = _first_paid_amount(block)
            if paid is not None:
                amount, unit = paid
                rates.append(PublishedRate(attributes=products[key], amount=amount, unit=unit))

    return rates


def _parse_block(buffer: list[str]) -> dict[str, Any] | None:
    text = "".join(buffer).rstrip().rstrip(",")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _reduce_attributes(product: dict[str, Any]) -> dict[str, str]:
    attributes = {
        field: str(value)
        for field, value in (product.get("attributes") or {}).items()
        if field in KEPT_ATTRIBUTES
    }
    family = product.get("productFamily")
    if family is not None:
        attributes["productFamily"] = str(family)
    return attributes


def _first_paid_amount(term_block: dict[str, Any]) -> tuple[float, str] | None:
    """The lowest tier with a non-zero USD price, and the unit it is quoted in."""
    for term in term_block.values():
        if not isinstance(term, dict):
            continue
        dimensions = sorted(
            (term.get("priceDimensions") or {}).values(),
            key=lambda dimension: _as_float(dimension.get("beginRange")),
        )
        for dimension in dimensions:
            amount = _as_float((dimension.get("pricePerUnit") or {}).get("USD"))
            if amount > 0:
                return amount, str(dimension.get("unit") or "")
    return None


def _as_float(raw: Any, default: float = 0.0) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default
