"""On-demand list prices from the AWS Price List Query API.

Used to estimate what a specific resource costs when Cost Explorer cannot attribute
spend to it, which is the common case: resource-level cost data is opt-in and only
retains 14 days. Anything priced here is labelled ``list_price_estimate`` so the UI
never presents it as a billed amount.

Every figure comes from AWS. There is no table of rates in this file on purpose: a stale
hardcoded price is indistinguishable from a real one once it reaches a dashboard. Rates
are looked up in two ways, in order:

1. ``pricing:GetProducts``, the Price List Query API.
2. The price list files AWS publishes without authentication, for roles that are not
   granted that action. See :mod:`finops.aws.price_list`.

If neither answers, the lookup returns ``None``, the resource is left unpriced, and the
scan records why.

Prices are cached on disk because the Price List API is slow, is only served from a
couple of regions, and returns figures that change rarely.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from finops.aws.errors import NoteCollector
from finops.aws.price_list import PublicPriceList, usage_type_matches
from finops.aws.session import AwsContext

logger = logging.getLogger(__name__)

HOURS_PER_MONTH = 730.0

# The Price List API is only served from these regions.
PRICING_API_REGIONS = ("us-east-1", "ap-south-1")

# gp3 includes a free allowance; only the excess is billable.
GP3_FREE_IOPS = 3000
GP3_FREE_THROUGHPUT_MIBPS = 125

# EFS provisioned throughput is billed only above the baseline that stored data already
# earns: AWS includes 50 KB/s per GB held in the Standard class, so the first 20 GB carry
# 1 MiB/s at no extra charge. Provisioning below that baseline costs nothing.
EFS_BASELINE_MIBPS_PER_GB = 1 / 20

# EFS storage classes as the price list names them, per tier and file system kind. The
# published files carry a single Archive rate per region, which One Zone archive data is
# billed at too.
EFS_STORAGE_CLASSES: dict[tuple[bool, str], tuple[str, str]] = {
    (False, "standard"): ("General Purpose", r"TimedStorage-ByteHrs"),
    (False, "ia"): ("Infrequent Access", r"IATimedStorage-ByteHrs"),
    (False, "archive"): ("Archive", r"ArchiveTimedStorage-ByteHrs"),
    (True, "standard"): ("One Zone-General Purpose", r"TimedStorage-Z-ByteHrs"),
    (True, "ia"): ("One Zone-Infrequent Access", r"IATimedStorage-Z-ByteHrs"),
    (True, "archive"): ("Archive", r"ArchiveTimedStorage-ByteHrs"),
}

# Elastic throughput moves the cost of reads and writes out of the storage rate, so cold
# storage is published at its own lower figure. Standard and Archive are unaffected.
EFS_ELASTIC_STORAGE_CLASSES: dict[tuple[bool, str], tuple[str, str]] = {
    (False, "ia"): ("Infrequent Access-ET", r"IATimedStorage-ET-ByteHrs"),
}

# A rate as AWS published it: the amount and the unit it was quoted in, kept together
# because the two are only meaningful side by side. Stored as a pair so it round-trips
# through the JSON cache.
Quote = Sequence[Any]

# AWS advertises a couple of charges in one unit and publishes them in another. gp3
# throughput is described as "$0.04 per provisioned MiBps-month" and published as 40.96
# with unit GiBps-mo. Converting is not a price assumption; the price is still AWS's.
_UNIT_CONVERSIONS = {("gibps-mo", "mibps-mo"): 1 / 1024}

# Transit gateway attachments are all $0.05/hour today, but each attachment kind is
# published as its own charge, keyed by the operation, so each is looked up on its own.
TRANSIT_GATEWAY_OPERATIONS = {
    "vpc": "TransitGatewayVPC",
    "vpn": "TransitGatewayVPN",
    "direct-connect-gateway": "TransitGatewayDirectConnect",
    "peering": "TransitGatewayPeering",
    "connect": "TransitGatewayConnect",
}

# What CloudWatch charges for an alarm depends on the kind, and a composite alarm costs
# five times a standard one.
CLOUDWATCH_ALARM_USAGE_TYPES = {
    "standard": "CW:AlarmMonitorUsage",
    "high_resolution": "CW:HighResAlarmMonitorUsage",
    "composite": "CW:CompositeAlarmMonitorUsage",
}

# RDS storage is named differently in the price list than in the RDS API.
RDS_VOLUME_TYPES = {
    "gp2": "General Purpose",
    "gp3": "General Purpose-GP3",
    "io1": "Provisioned IOPS",
    "io2": "Provisioned IOPS-IO2",
    "standard": "Magnetic",
    "magnetic": "Magnetic",
}


@dataclass(frozen=True)
class Price:
    """A unit price as published by AWS, with the unit it is charged in."""

    amount: float
    unit: str
    source: str = "pricing-api"

    @property
    def monthly(self) -> float:
        """Convert an hourly rate to a monthly one; pass through monthly rates."""
        return self.amount * HOURS_PER_MONTH if self.unit.lower().startswith("hr") else self.amount


class PricingClient:
    """Looks up on-demand list prices, cached on disk, with no invented rates."""

    def __init__(
        self,
        aws: AwsContext,
        notes: NoteCollector | None = None,
        cache_path: Path | None = None,
        public_price_list: PublicPriceList | None = None,
    ) -> None:
        self.aws = aws
        self.notes = notes or NoteCollector()
        self.cache_path = cache_path or (Path(aws.settings.db_path).parent / "pricing-cache.json")
        self._cache: dict[str, float | None] = self._load_cache()
        self._lock = threading.Lock()
        self._api_available = True
        self._misses: set[str] = set()
        self._public = public_price_list
        self._used_public = False

    @property
    def api_available(self) -> bool:
        """False once the Price List API has refused or failed a request."""
        return self._api_available

    @property
    def used_public_price_list(self) -> bool:
        """True when a rate came from the published files rather than the API."""
        return self._used_public

    @property
    def unresolved(self) -> list[str]:
        """Lookups AWS answered, but with nothing that matched."""
        return sorted(self._misses)

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
    def _cache_key(service_code: str, filters: dict[str, str], usage_type: str | None) -> str:
        payload = (
            service_code
            + "|"
            + "|".join(f"{k}={v}" for k, v in sorted(filters.items()))
            + f"|usage={usage_type or ''}"
        )
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:20]

    # ------------------------------------------------------------ api plumbing

    def _price(
        self,
        service_code: str,
        filters: dict[str, str],
        unit: str,
        *,
        usage_type: str | None = None,
    ) -> Price | None:
        """The on-demand unit price for one charge, or None if AWS did not supply one."""
        key = self._cache_key(service_code, filters, usage_type)
        with self._lock:
            if key in self._cache:
                cached = self._cache[key]
                if cached is None:
                    return None
                return Price(_in_unit(cached, unit), unit, self._source())

        quoted, source = self._lookup(service_code, filters, usage_type)
        with self._lock:
            self._cache[key] = quoted
        if quoted is None:
            return None
        return Price(_in_unit(quoted, unit), unit, source)

    def _lookup(
        self, service_code: str, filters: dict[str, str], usage_type: str | None
    ) -> tuple[Quote | None, str]:
        if self._api_available:
            quoted = self._from_api(service_code, filters, usage_type)
            if quoted is not None:
                return quoted, "pricing-api"
            if self._api_available:
                # The API answered and nothing matched, so the filters no longer describe
                # how AWS publishes this charge. The published files would agree.
                self._record_miss(service_code, filters, usage_type)
                return None, "pricing-api"

        region = filters.get("regionCode")
        if self._public is None or not region:
            return None, "pricing-api"
        quoted = self._public.rate(service_code, region, filters, usage_type)
        if quoted is None:
            self._record_miss(service_code, filters, usage_type)
            return None, "public-price-list"
        with self._lock:
            self._used_public = True
        return quoted, "public-price-list"

    def _from_api(
        self, service_code: str, filters: dict[str, str], usage_type: str | None
    ) -> Quote | None:
        try:
            client = self.aws.client("pricing", PRICING_API_REGIONS[0])
            response = client.get_products(
                ServiceCode=service_code,
                Filters=[
                    {"Type": "TERM_MATCH", "Field": field, "Value": value}
                    for field, value in filters.items()
                ],
                MaxResults=100,
            )
        except (ClientError, BotoCoreError) as exc:
            self.notes.add_from_exception("pricing:GetProducts", exc)
            with self._lock:
                # One failure is almost always a permission problem, so stop retrying.
                self._api_available = False
            return None
        return _first_paid_rate(response.get("PriceList", []), usage_type)

    def _record_miss(
        self, service_code: str, filters: dict[str, str], usage_type: str | None
    ) -> None:
        descriptor = usage_type or filters.get("productFamily") or "?"
        logger.warning("No list price matched %s %s (%s)", service_code, descriptor, filters)
        with self._lock:
            self._misses.add(f"{service_code} {descriptor}")

    def _source(self) -> str:
        return (
            "public-price-list" if self._used_public and not self._api_available else "pricing-api"
        )

    # ------------------------------------------------------------- ec2 and rds

    def ec2_instance_hourly(
        self,
        region: str,
        instance_type: str,
        *,
        operating_system: str = "Linux",
        tenancy: str = "Shared",
    ) -> Price | None:
        return self._price(
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
            "Hrs",
        )

    def rds_instance_hourly(
        self,
        region: str,
        instance_class: str,
        engine: str,
        *,
        multi_az: bool = False,
    ) -> Price | None:
        return self._price(
            "AmazonRDS",
            {
                "regionCode": region,
                "instanceType": instance_class,
                "databaseEngine": _normalize_rds_engine(engine),
                "deploymentOption": "Multi-AZ" if multi_az else "Single-AZ",
            },
            "Hrs",
            usage_type="Multi-AZUsage:.+" if multi_az else "InstanceUsage:.+",
        )

    def rds_storage_gb_month(
        self, region: str, storage_type: str = "gp2", *, multi_az: bool = False
    ) -> Price | None:
        volume_type = RDS_VOLUME_TYPES.get(storage_type)
        if volume_type is None:
            return None
        return self._price(
            "AmazonRDS",
            {
                "regionCode": region,
                "productFamily": "Database Storage",
                "volumeType": volume_type,
            },
            "GB-Mo",
            # The Multi-AZ rate is published separately and already covers both copies.
            # Mirror storage belongs to Multi-AZ clusters with readable standbys.
            usage_type=r"RDS:Multi-AZ-\S+" if multi_az else r"RDS:(?!Multi-AZ|Mirror)\S+",
        )

    def rds_backup_gb_month(self, region: str) -> Price | None:
        return self._price(
            "AmazonRDS",
            {"regionCode": region, "productFamily": "Storage Snapshot"},
            "GB-Mo",
            usage_type="RDS:ChargedBackupUsage",
        )

    # ----------------------------------------------------------------- storage

    def ebs_gb_month(self, region: str, volume_type: str) -> Price | None:
        return self._price(
            "AmazonEC2",
            {
                "regionCode": region,
                "productFamily": "Storage",
                "volumeApiName": volume_type,
            },
            "GB-Mo",
            usage_type=r"EBS:VolumeUsage(\.\S+)?",
        )

    def ebs_iops_month(self, region: str, volume_type: str) -> Price | None:
        return self._price(
            "AmazonEC2",
            {
                "regionCode": region,
                "productFamily": "System Operation",
                "volumeApiName": volume_type,
            },
            "IOPS-Mo",
            # io2 IOPS are tiered; the first tier is the one a volume starts billing at.
            usage_type=r"EBS:VolumeP-IOPS\.[^.]+(\.tier1)?",
        )

    def ebs_throughput_month(self, region: str, volume_type: str = "gp3") -> Price | None:
        return self._price(
            "AmazonEC2",
            {
                "regionCode": region,
                "productFamily": "Provisioned Throughput",
                "volumeApiName": volume_type,
            },
            "MiBps-Mo",
            usage_type=r"EBS:VolumeP-Throughput\.\S+",
        )

    def snapshot_gb_month(self, region: str) -> Price | None:
        return self._price(
            "AmazonEC2",
            {"regionCode": region, "productFamily": "Storage Snapshot"},
            "GB-Mo",
            usage_type="EBS:SnapshotUsage",
        )

    def efs_storage_gb_month(
        self, region: str, tier: str = "standard", *, one_zone: bool = False, elastic: bool = False
    ) -> Price | None:
        """One EFS storage tier: Standard, Infrequent Access, or Archive."""
        published = EFS_ELASTIC_STORAGE_CLASSES.get((one_zone, tier)) if elastic else None
        if published is None:
            published = EFS_STORAGE_CLASSES.get((one_zone, tier))
        if published is None:
            return None
        storage_class, usage_type = published
        return self._price(
            "AmazonEFS",
            {
                "regionCode": region,
                "productFamily": "Storage",
                "storageClass": storage_class,
            },
            "GB-Mo",
            # Reads and writes against the cold tiers share the storage class and are
            # charged per GB transferred, so the usage type has to pin the stored rate.
            usage_type=usage_type,
        )

    def efs_provisioned_throughput_month(self, region: str) -> Price | None:
        return self._price(
            "AmazonEFS",
            {"regionCode": region, "productFamily": "Provisioned Throughput"},
            "MiBps-Mo",
            # The throughput included with Standard storage is published in the same
            # family at $0.00.
            usage_type="ProvisionedTP-MiBpsHrs",
        )

    def s3_standard_gb_month(self, region: str) -> Price | None:
        return self._price(
            "AmazonS3",
            {
                "regionCode": region,
                "productFamily": "Storage",
                "volumeType": "Standard",
                "storageClass": "General Purpose",
            },
            "GB-Mo",
            usage_type="TimedStorage-ByteHrs",
        )

    def logs_storage_gb_month(self, region: str) -> Price | None:
        return self._price(
            "AmazonCloudWatch",
            {"regionCode": region, "productFamily": "Storage Snapshot"},
            "GB-Mo",
            # Infrequent Access log classes publish their own TimedStorage-IA usage type.
            usage_type="TimedStorage-ByteHrs",
        )

    # ---------------------------------------------------------------- network

    def nat_gateway_hourly(self, region: str) -> Price | None:
        return self._price(
            "AmazonEC2",
            {"regionCode": region, "productFamily": "NAT Gateway"},
            "Hrs",
            usage_type="(Regional)?NatGateway-Hours",
        )

    def nat_gateway_gb(self, region: str) -> Price | None:
        return self._price(
            "AmazonEC2",
            {"regionCode": region, "productFamily": "NAT Gateway"},
            "GB",
            usage_type="(Regional)?NatGateway-Bytes",
        )

    def load_balancer_hourly(self, region: str, lb_type: str = "application") -> Price | None:
        family = {
            "application": "Load Balancer-Application",
            "network": "Load Balancer-Network",
            "gateway": "Load Balancer-Gateway",
            "classic": "Load Balancer",
        }.get(lb_type, "Load Balancer-Application")
        return self._price(
            "AWSELB",
            {"regionCode": region, "productFamily": family},
            "Hrs",
            # Outposts and Traffic Shaping usage types share the product family.
            usage_type="LoadBalancerUsage",
        )

    def public_ipv4_hourly(self, region: str, *, in_use: bool = True) -> Price | None:
        # Charged by VPC rather than EC2 since the 2024 change that made every public
        # IPv4 address billable, whether attached to anything or not.
        return self._price(
            "AmazonVPC",
            {"regionCode": region, "group": "VPCPublicIPv4Address"},
            "Hrs",
            usage_type="PublicIPv4:InUseAddress" if in_use else "PublicIPv4:IdleAddress",
        )

    def transit_gateway_attachment_hourly(self, region: str, kind: str = "vpc") -> Price | None:
        """Per attachment, per hour. The gateway itself carries no charge of its own."""
        operation = TRANSIT_GATEWAY_OPERATIONS.get(kind)
        if operation is None:
            return None
        return self._price(
            # These products carry no product family, so the operation is what separates
            # an attachment hour from the data it processes.
            "AmazonVPC",
            {"regionCode": region, "operation": operation},
            "Hrs",
            usage_type="TransitGateway-Hours",
        )

    def transit_gateway_gb(self, region: str, kind: str = "vpc") -> Price | None:
        operation = TRANSIT_GATEWAY_OPERATIONS.get(kind)
        if operation is None:
            return None
        return self._price(
            "AmazonVPC",
            {"regionCode": region, "operation": operation},
            "GB",
            usage_type="TransitGateway-Bytes",
        )

    def vpc_endpoint_hourly(
        self, region: str, *, gateway_load_balancer: bool = False
    ) -> Price | None:
        """Per endpoint per availability zone, per hour. Gateway endpoints are free."""
        return self._price(
            "AmazonVPC",
            {"regionCode": region, "productFamily": "VpcEndpoint", "operation": "VpcEndpoint"},
            "Hrs",
            usage_type="VpcEndpoint-GWLBE-Hours" if gateway_load_balancer else "VpcEndpoint-Hours",
        )

    def vpc_endpoint_gb(self, region: str, *, gateway_load_balancer: bool = False) -> Price | None:
        return self._price(
            "AmazonVPC",
            {"regionCode": region, "productFamily": "VpcEndpoint", "operation": "VpcEndpoint"},
            "GB",
            usage_type="VpcEndpoint-GWLBE-Bytes" if gateway_load_balancer else "VpcEndpoint-Bytes",
        )

    def vpn_connection_hourly(self, region: str) -> Price | None:
        """A site-to-site VPN connection, charged per hour it exists."""
        return self._price(
            "AmazonVPC",
            {
                "regionCode": region,
                "productFamily": "Cloud Connectivity",
                "operation": "CreateVpnConnection",
            },
            "Hrs",
            # Accelerated and large-bandwidth connections publish their own usage types.
            usage_type=r"VPN-Usage-Hours:ipsec\.1",
        )

    def client_vpn_endpoint_hourly(self, region: str) -> Price | None:
        """Charged per associated subnet-hour, whether anyone connects or not."""
        return self._price(
            "AmazonVPC",
            {"regionCode": region, "operation": "ClientVPNEndpoints"},
            "Hrs",
            usage_type="ClientVPN-EndpointHours",
        )

    # ------------------------------------------------------- keys and secrets

    def kms_key_month(self, region: str) -> Price | None:
        """A customer managed key. AWS managed keys carry no charge."""
        return self._price(
            "awskms",
            {"regionCode": region, "productFamily": "Encryption Key"},
            "Keys",
            # KMS prefixes its usage types with the full region name rather than a code.
            usage_type=r"(?:.+-)?KMS-Keys",
        )

    def secret_month(self, region: str) -> Price | None:
        return self._price(
            "AWSSecretsManager",
            {"regionCode": region, "productFamily": "Secret"},
            "Secrets",
            usage_type="AWSSecretsManager-Secrets",
        )

    def private_ca_month(self, region: str, *, short_lived: bool = False) -> Price | None:
        """A private certificate authority, billed monthly until it is deleted."""
        return self._price(
            "AWSCertificateManager",
            {"regionCode": region, "productFamily": "AWS Certificate Manager"},
            "CertificateAuthorities",
            usage_type=("ShortLivedCertificatePrivateCA" if short_lived else "PaidPrivateCA"),
        )

    # ------------------------------------------------- registries and telemetry

    def ecr_storage_gb_month(self, region: str) -> Price | None:
        return self._price(
            "AmazonECR",
            {"regionCode": region, "productFamily": "EC2 Container Registry"},
            "GB-Mo",
            usage_type="TimedStorage-ByteHrs",
        )

    def cloudwatch_alarm_month(self, region: str, kind: str = "standard") -> Price | None:
        """One alarm, per month. High resolution and composite alarms cost more."""
        usage_type = CLOUDWATCH_ALARM_USAGE_TYPES.get(kind)
        if usage_type is None:
            return None
        return self._price(
            "AmazonCloudWatch",
            {"regionCode": region, "productFamily": "Alarm"},
            "Alarms",
            usage_type=usage_type,
        )

    def sns_request(self, region: str) -> Price | None:
        """Per published message. Delivery to most endpoints is charged on top."""
        return self._price(
            "AmazonSNS",
            {"regionCode": region, "productFamily": "API Request"},
            "Requests",
            usage_type="Requests-Tier1",
        )

    def sqs_request(self, region: str, *, fifo: bool = False) -> Price | None:
        """Per API request. Every send, receive, and delete counts as one."""
        return self._price(
            "AWSQueueService",
            {"regionCode": region, "productFamily": "API Request"},
            "Requests",
            # The same charge is published as Tier1 in some regions and RBP in others.
            usage_type=r"Requests-FIFO-(?:Tier1|RBP)" if fifo else r"Requests-(?:Tier1|RBP)",
        )

    # --------------------------------------------------------------- compute

    def eks_cluster_hourly(self, region: str) -> Price | None:
        return self._price(
            "AmazonEKS",
            {"regionCode": region, "productFamily": "Compute"},
            "Hrs",
            # Excludes Outposts and extended-version-support cluster hours.
            usage_type="AmazonEKS-Hours:perCluster",
        )

    def lambda_gb_second(self, region: str, *, provisioned: bool = False) -> Price | None:
        group = "AWS-Lambda-Provisioned-Concurrency" if provisioned else "AWS-Lambda-Duration"
        return self._price(
            "AWSLambda",
            {"regionCode": region, "group": group},
            "GB-Second",
            usage_type=("Lambda-Provisioned-Concurrency" if provisioned else "Lambda-GB-Second"),
        )

    def lambda_request(self, region: str) -> Price | None:
        return self._price(
            "AWSLambda",
            {"regionCode": region, "group": "AWS-Lambda-Requests"},
            "Request",
            usage_type="Request",
        )

    def dynamodb_capacity_hourly(self, region: str, kind: str) -> Price | None:
        """Provisioned read or write capacity, per unit-hour."""
        group = "DDB-ReadUnits" if kind == "read" else "DDB-WriteUnits"
        unit = "ReadCapacityUnit" if kind == "read" else "WriteCapacityUnit"
        return self._price(
            "AmazonDynamoDB",
            {"regionCode": region, "group": group},
            "Hrs",
            usage_type=f"{unit}-Hrs",
        )

    def dynamodb_storage_gb_month(self, region: str) -> Price | None:
        return self._price(
            "AmazonDynamoDB",
            {
                "regionCode": region,
                "productFamily": "Database Storage",
                "volumeType": "Amazon DynamoDB - Indexed DataStore",
            },
            "GB-Mo",
            usage_type="TimedStorage-ByteHrs",
        )

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

    def efs_file_system_monthly(
        self,
        region: str,
        *,
        standard_gb: float,
        ia_gb: float = 0.0,
        archive_gb: float = 0.0,
        one_zone: bool = False,
        throughput_mode: str = "bursting",
        provisioned_mibps: float | None = None,
    ) -> float | None:
        """Stored data across the tiers it sits in, plus billable provisioned throughput.

        Reads and writes are excluded: in Bursting and Provisioned modes they are only
        charged against the cold tiers, and in Elastic mode they are the whole bill. Either
        way they are usage rather than standing cost, and Cost Explorer is the honest source
        for them.
        """
        elastic = throughput_mode == "elastic"
        standard = self.efs_storage_gb_month(region, "standard", one_zone=one_zone, elastic=elastic)
        if standard is None:
            return None
        total = standard.amount * standard_gb

        for tier, size_gb in (("ia", ia_gb), ("archive", archive_gb)):
            if size_gb <= 0:
                continue
            price = self.efs_storage_gb_month(region, tier, one_zone=one_zone, elastic=elastic)
            if price:
                total += price.amount * size_gb

        billable = efs_billable_throughput_mibps(standard_gb, throughput_mode, provisioned_mibps)
        if billable > 0:
            throughput = self.efs_provisioned_throughput_month(region)
            if throughput:
                total += throughput.amount * billable
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


def efs_billable_throughput_mibps(
    standard_gb: float, throughput_mode: str | None, provisioned_mibps: float | None
) -> float:
    """The share of provisioned throughput that is actually charged for.

    Only Provisioned mode carries a throughput charge, and only for what is provisioned
    above the baseline the Standard storage already includes. A file system provisioned
    below its own baseline pays nothing extra and quietly runs on Bursting instead.
    """
    if throughput_mode != "provisioned" or not provisioned_mibps:
        return 0.0
    included = max(standard_gb, 0.0) * EFS_BASELINE_MIBPS_PER_GB
    return max(provisioned_mibps - included, 0.0)


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


def _first_paid_rate(price_list: list[Any], usage_type: str | None = None) -> Quote | None:
    """The first non-zero USD on-demand rate, preferring the lowest tier.

    Tiers are skipped while they are free, so a volume-discounted charge yields the rate
    the first billable unit is charged at, and a free allowance never reads as $0.
    """
    pattern = re.compile(usage_type) if usage_type else None
    for entry in price_list:
        try:
            product = json.loads(entry) if isinstance(entry, str) else entry
        except json.JSONDecodeError:
            continue
        attributes = (product.get("product") or {}).get("attributes") or {}
        if pattern and not usage_type_matches(attributes, pattern):
            continue
        for term in ((product.get("terms") or {}).get("OnDemand") or {}).values():
            dimensions = sorted(
                (term.get("priceDimensions") or {}).values(),
                key=lambda dimension: _as_float(dimension.get("beginRange")),
            )
            for dimension in dimensions:
                value = _as_float((dimension.get("pricePerUnit") or {}).get("USD"), default=0.0)
                # Price lists include $0.00 entries for free tiers and placeholders.
                if value > 0:
                    return value, str(dimension.get("unit") or "")
    return None


def _in_unit(quoted: Quote | float, unit: str) -> float:
    """A published amount, converted when AWS quoted the charge in a different unit."""
    if isinstance(quoted, int | float):
        return float(quoted)  # A cache written before units were recorded.
    amount, published = float(quoted[0]), str(quoted[1])
    scale = _UNIT_CONVERSIONS.get((published.lower(), unit.lower()))
    return amount * scale if scale else amount


def _as_float(raw: Any, default: float = 0.0) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def build_pricing(
    aws: AwsContext, notes: NoteCollector | None = None, cache_path: Path | None = None
) -> PricingClient:
    """A pricing client with the published-file fallback wired up per configuration."""
    public = None
    if aws.settings.public_price_list:
        public = PublicPriceList(Path(aws.settings.db_path).parent / "price-list")
    return PricingClient(aws, notes, cache_path=cache_path, public_price_list=public)
