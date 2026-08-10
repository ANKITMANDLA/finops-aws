"""On-demand list prices from the AWS Price List Query API.

Used to estimate what a specific resource costs when Cost Explorer cannot attribute
spend to it, which is the common case: resource-level cost data is opt-in and only
retains 14 days. Anything priced here is labelled ``list_price_estimate`` so the UI
never presents it as a billed amount.

Prices are cached on disk because the Price List API is slow, is only served from a
couple of regions, and returns figures that change rarely.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from finops.aws.errors import NoteCollector
from finops.aws.session import AwsContext

logger = logging.getLogger(__name__)

HOURS_PER_MONTH = 730.0

# The Price List API is only served from these regions.
PRICING_API_REGIONS = ("us-east-1", "ap-south-1")

# us-east-1 list prices, used when the Price List API is unavailable (denied, throttled,
# or unreachable). Deliberately limited to charges that vary little between regions;
# instance-hour prices have no sane default and return None instead.
FALLBACK_PRICES: dict[str, float] = {
    "ebs:gp3:gb-month": 0.08,
    "ebs:gp2:gb-month": 0.10,
    "ebs:io1:gb-month": 0.125,
    "ebs:io2:gb-month": 0.125,
    "ebs:st1:gb-month": 0.045,
    "ebs:sc1:gb-month": 0.015,
    "ebs:standard:gb-month": 0.05,
    "ebs:gp3:iops-month": 0.005,
    "ebs:io1:iops-month": 0.065,
    "ebs:io2:iops-month": 0.065,
    "ebs:gp3:throughput-month": 0.040,
    "snapshot:gb-month": 0.05,
    "natgw:hour": 0.045,
    "elb:application:hour": 0.0225,
    "elb:network:hour": 0.0225,
    "elb:gateway:hour": 0.0125,
    "elb:classic:hour": 0.025,
    "eip:hour": 0.005,
    "eks:cluster-hour": 0.10,
    "logs:storage-gb-month": 0.03,
}

# gp3 includes a free allowance; only the excess is billable.
GP3_FREE_IOPS = 3000
GP3_FREE_THROUGHPUT_MIBPS = 125


@dataclass(frozen=True)
class Price:
    """A unit price and where the number came from."""

    amount: float
    unit: str
    source: str  # "pricing-api" or "fallback"

    @property
    def monthly(self) -> float:
        """Convert an hourly rate to a monthly one; pass through monthly rates."""
        return self.amount * HOURS_PER_MONTH if self.unit.lower().startswith("hr") else self.amount


class PricingClient:
    """Looks up on-demand list prices, with a disk cache and static fallbacks."""

    def __init__(
        self,
        aws: AwsContext,
        notes: NoteCollector | None = None,
        cache_path: Path | None = None,
    ) -> None:
        self.aws = aws
        self.notes = notes or NoteCollector()
        self.cache_path = cache_path or (Path(aws.settings.db_path).parent / "pricing-cache.json")
        self._cache: dict[str, float | None] = self._load_cache()
        self._lock = threading.Lock()
        self._api_available = True

    # ----------------------------------------------------------------- caching

    def _load_cache(self) -> dict[str, float | None]:
        try:
            if self.cache_path.exists():
                return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("Could not read pricing cache: %s", exc)
        return {}

    def save_cache(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self._cache, indent=0), encoding="utf-8")
        except OSError as exc:
            logger.debug("Could not write pricing cache: %s", exc)

    @staticmethod
    def _cache_key(service_code: str, filters: dict[str, str]) -> str:
        payload = service_code + "|" + "|".join(f"{k}={v}" for k, v in sorted(filters.items()))
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:20]

    # ------------------------------------------------------------ api plumbing

    def _query(self, service_code: str, filters: dict[str, str]) -> float | None:
        """Return the on-demand USD unit price for the first matching product."""
        key = self._cache_key(service_code, filters)
        with self._lock:
            if key in self._cache:
                return self._cache[key]
            if not self._api_available:
                return None

        price = None
        try:
            client = self.aws.client("pricing", PRICING_API_REGIONS[0])
            response = client.get_products(
                ServiceCode=service_code,
                Filters=[
                    {"Type": "TERM_MATCH", "Field": field, "Value": value}
                    for field, value in filters.items()
                ],
                MaxResults=20,
            )
            price = _extract_on_demand_usd(response.get("PriceList", []))
        except (ClientError, BotoCoreError) as exc:
            self.notes.add_from_exception("pricing:GetProducts", exc)
            with self._lock:
                # One failure is almost always a permission problem, so stop retrying.
                self._api_available = False
            return None

        with self._lock:
            self._cache[key] = price
        return price

    def _resolve(
        self, service_code: str, filters: dict[str, str], fallback_key: str | None, unit: str
    ) -> Price | None:
        amount = self._query(service_code, filters)
        if amount is not None:
            return Price(amount=amount, unit=unit, source="pricing-api")
        if fallback_key and fallback_key in FALLBACK_PRICES:
            return Price(amount=FALLBACK_PRICES[fallback_key], unit=unit, source="fallback")
        return None

    # ------------------------------------------------------------- ec2 and rds

    def ec2_instance_hourly(
        self,
        region: str,
        instance_type: str,
        *,
        operating_system: str = "Linux",
        tenancy: str = "Shared",
    ) -> Price | None:
        return self._resolve(
            "AmazonEC2",
            {
                "regionCode": region,
                "instanceType": instance_type,
                "operatingSystem": _normalize_os(operating_system),
                "tenancy": "Shared" if tenancy == "default" else tenancy.capitalize(),
                "preInstalledSw": "NA",
                "capacitystatus": "Used",
                "marketoption": "OnDemand",
                "licenseModel": "No License required",
            },
            fallback_key=None,
            unit="Hrs",
        )

    def rds_instance_hourly(
        self,
        region: str,
        instance_class: str,
        engine: str,
        *,
        multi_az: bool = False,
    ) -> Price | None:
        return self._resolve(
            "AmazonRDS",
            {
                "regionCode": region,
                "instanceType": instance_class,
                "databaseEngine": _normalize_rds_engine(engine),
                "deploymentOption": "Multi-AZ" if multi_az else "Single-AZ",
            },
            fallback_key=None,
            unit="Hrs",
        )

    # ----------------------------------------------------------------- storage

    def ebs_gb_month(self, region: str, volume_type: str) -> Price | None:
        return self._resolve(
            "AmazonEC2",
            {
                "regionCode": region,
                "productFamily": "Storage",
                "volumeApiName": volume_type,
            },
            fallback_key=f"ebs:{volume_type}:gb-month",
            unit="GB-Mo",
        )

    def ebs_iops_month(self, region: str, volume_type: str) -> Price | None:
        return self._resolve(
            "AmazonEC2",
            {
                "regionCode": region,
                "productFamily": "System Operation",
                "volumeApiName": volume_type,
                "group": "EBS IOPS",
            },
            fallback_key=f"ebs:{volume_type}:iops-month",
            unit="IOPS-Mo",
        )

    def ebs_throughput_month(self, region: str, volume_type: str = "gp3") -> Price | None:
        return self._resolve(
            "AmazonEC2",
            {
                "regionCode": region,
                "productFamily": "Provisioned Throughput",
                "volumeApiName": volume_type,
            },
            fallback_key=f"ebs:{volume_type}:throughput-month",
            unit="MiBps-Mo",
        )

    def snapshot_gb_month(self, region: str) -> Price | None:
        return self._resolve(
            "AmazonEC2",
            {
                "regionCode": region,
                "productFamily": "Storage Snapshot",
                "snapshotarchivefeetype": "",
            },
            fallback_key="snapshot:gb-month",
            unit="GB-Mo",
        )

    def logs_storage_gb_month(self, region: str) -> Price:
        price = self._resolve(
            "AmazonCloudWatch",
            {"regionCode": region, "productFamily": "Storage Snapshot"},
            fallback_key="logs:storage-gb-month",
            unit="GB-Mo",
        )
        return price or Price(FALLBACK_PRICES["logs:storage-gb-month"], "GB-Mo", "fallback")

    # ---------------------------------------------------------------- network

    def nat_gateway_hourly(self, region: str) -> Price:
        price = self._resolve(
            "AmazonEC2",
            {
                "regionCode": region,
                "productFamily": "NAT Gateway",
                "group": "NGW:NatGateway",
                "usagetype": _regional_usage_type(region, "NatGateway-Hours"),
            },
            fallback_key="natgw:hour",
            unit="Hrs",
        )
        return price or Price(FALLBACK_PRICES["natgw:hour"], "Hrs", "fallback")

    def load_balancer_hourly(self, region: str, lb_type: str = "application") -> Price:
        family = {
            "application": "Load Balancer-Application",
            "network": "Load Balancer-Network",
            "gateway": "Load Balancer-Gateway",
            "classic": "Load Balancer",
        }.get(lb_type, "Load Balancer-Application")
        price = self._resolve(
            "AWSELB",
            {"regionCode": region, "productFamily": family},
            fallback_key=f"elb:{lb_type}:hour",
            unit="Hrs",
        )
        return price or Price(FALLBACK_PRICES.get(f"elb:{lb_type}:hour", 0.0225), "Hrs", "fallback")

    def public_ipv4_hourly(self, region: str) -> Price:
        price = self._resolve(
            "AmazonEC2",
            {
                "regionCode": region,
                "productFamily": "IP Address",
                "group": "VPCPublicIPv4Address",
            },
            fallback_key="eip:hour",
            unit="Hrs",
        )
        return price or Price(FALLBACK_PRICES["eip:hour"], "Hrs", "fallback")

    def eks_cluster_hourly(self, region: str) -> Price:
        price = self._resolve(
            "AmazonEKS",
            {"regionCode": region, "tiertype": "Standard Kubernetes Version"},
            fallback_key="eks:cluster-hour",
            unit="Hrs",
        )
        return price or Price(FALLBACK_PRICES["eks:cluster-hour"], "Hrs", "fallback")

    # ------------------------------------------------------------- composites

    def ebs_volume_monthly(
        self,
        region: str,
        volume_type: str,
        size_gb: float,
        *,
        iops: int | None = None,
        throughput_mibps: int | None = None,
    ) -> float | None:
        """Full monthly cost of a volume: capacity plus billable IOPS and throughput."""
        capacity = self.ebs_gb_month(region, volume_type)
        if capacity is None:
            return None
        total = capacity.amount * size_gb

        if volume_type == "gp3":
            billable_iops = max((iops or 0) - GP3_FREE_IOPS, 0)
            billable_throughput = max((throughput_mibps or 0) - GP3_FREE_THROUGHPUT_MIBPS, 0)
        elif volume_type in {"io1", "io2"}:
            billable_iops = iops or 0
            billable_throughput = 0
        else:
            billable_iops = 0
            billable_throughput = 0

        if billable_iops:
            iops_price = self.ebs_iops_month(region, volume_type)
            if iops_price:
                total += iops_price.amount * billable_iops
        if billable_throughput:
            throughput_price = self.ebs_throughput_month(region, volume_type)
            if throughput_price:
                total += throughput_price.amount * billable_throughput
        return round(total, 4)

    def ec2_instance_monthly(
        self, region: str, instance_type: str, *, operating_system: str = "Linux"
    ) -> float | None:
        price = self.ec2_instance_hourly(region, instance_type, operating_system=operating_system)
        return round(price.monthly, 4) if price else None

    def rds_instance_monthly(
        self, region: str, instance_class: str, engine: str, *, multi_az: bool = False
    ) -> float | None:
        price = self.rds_instance_hourly(region, instance_class, engine, multi_az=multi_az)
        return round(price.monthly, 4) if price else None


def _normalize_os(operating_system: str) -> str:
    lowered = (operating_system or "linux").lower()
    if "windows" in lowered:
        return "Windows"
    if "rhel" in lowered or "red hat" in lowered:
        return "RHEL"
    if "suse" in lowered:
        return "SUSE"
    return "Linux"


def _normalize_rds_engine(engine: str) -> str:
    lowered = (engine or "").lower()
    mapping = {
        "aurora-mysql": "Aurora MySQL",
        "aurora-postgresql": "Aurora PostgreSQL",
        "mysql": "MySQL",
        "mariadb": "MariaDB",
        "postgres": "PostgreSQL",
        "oracle": "Oracle",
        "sqlserver": "SQL Server",
    }
    for prefix, name in mapping.items():
        if lowered.startswith(prefix):
            return name
    return engine or "MySQL"


def _regional_usage_type(region: str, suffix: str) -> str:
    """Usage types carry a region prefix everywhere except us-east-1."""
    prefixes = {
        "us-east-2": "USE2",
        "us-west-1": "USW1",
        "us-west-2": "USW2",
        "eu-west-1": "EU",
        "eu-west-2": "EUW2",
        "eu-central-1": "EUC1",
        "ap-south-1": "APS3",
        "ap-southeast-1": "APS1",
        "ap-southeast-2": "APS2",
        "ap-northeast-1": "APN1",
        "ca-central-1": "CAN1",
        "sa-east-1": "SAE1",
    }
    prefix = prefixes.get(region)
    return f"{prefix}-{suffix}" if prefix else suffix


def _extract_on_demand_usd(price_list: list[Any]) -> float | None:
    """Pull the first non-zero USD on-demand unit price out of a Price List response."""
    for entry in price_list:
        try:
            product = json.loads(entry) if isinstance(entry, str) else entry
        except json.JSONDecodeError:
            continue
        on_demand = (product.get("terms") or {}).get("OnDemand") or {}
        for term in on_demand.values():
            for dimension in (term.get("priceDimensions") or {}).values():
                raw = (dimension.get("pricePerUnit") or {}).get("USD")
                if raw is None:
                    continue
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    continue
                # Price lists include $0.00 entries for free tiers and placeholders.
                if value > 0:
                    return value
    return None
