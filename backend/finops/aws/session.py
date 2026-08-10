"""Read-only boto3 session management.

A single :class:`AwsContext` is threaded through every collector. It owns the boto3
session, caches clients per (service, region), and centralizes the retry policy so
a wide multi-region scan does not trip API throttling.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from functools import cached_property

import boto3
from botocore.client import BaseClient
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from finops.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Services with a single global endpoint, or whose data is account-wide. Requests for
# these are pinned to the billing region so we never fan them out per region.
GLOBAL_SERVICES = frozenset(
    {
        "ce",
        "cost-optimization-hub",
        "organizations",
        "iam",
        "sts",
        "support",
        "pricing",
    }
)

# Fallback list used when ec2:DescribeRegions is denied.
FALLBACK_REGIONS = (
    "us-east-1",
    "us-east-2",
    "us-west-1",
    "us-west-2",
    "eu-west-1",
    "eu-west-2",
    "eu-central-1",
    "ap-south-1",
    "ap-southeast-1",
    "ap-southeast-2",
    "ap-northeast-1",
    "ca-central-1",
    "sa-east-1",
)


@dataclass
class AwsContext:
    """Holds credentials, region list, and a thread-safe client cache."""

    settings: Settings = field(default_factory=get_settings)
    _clients: dict[tuple[str, str], BaseClient] = field(default_factory=dict, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    @cached_property
    def session(self) -> boto3.Session:
        if self.settings.aws_profile:
            return boto3.Session(profile_name=self.settings.aws_profile)
        return boto3.Session()

    @cached_property
    def boto_config(self) -> BotoConfig:
        # Adaptive mode adds client-side rate limiting on top of retries, which matters
        # because a full scan issues thousands of Describe calls across many regions.
        return BotoConfig(
            retries={"max_attempts": 10, "mode": "adaptive"},
            connect_timeout=10,
            read_timeout=60,
            user_agent_extra="finops-agent/0.1.0",
        )

    def client(self, service: str, region: str | None = None) -> BaseClient:
        """Return a cached client. Global services ignore ``region``."""
        effective_region = (
            self.settings.billing_region
            if service in GLOBAL_SERVICES
            else (region or self.default_region)
        )
        key = (service, effective_region)
        with self._lock:
            client = self._clients.get(key)
            if client is None:
                client = self.session.client(
                    service, region_name=effective_region, config=self.boto_config
                )
                self._clients[key] = client
            return client

    @cached_property
    def default_region(self) -> str:
        return self.session.region_name or self.settings.billing_region

    @cached_property
    def account_id(self) -> str:
        try:
            return self.client("sts").get_caller_identity()["Account"]
        except (ClientError, BotoCoreError) as exc:
            logger.warning("Could not resolve account id: %s", exc)
            return "unknown"

    @cached_property
    def account_alias(self) -> str | None:
        try:
            aliases = self.client("iam").list_account_aliases().get("AccountAliases", [])
            return aliases[0] if aliases else None
        except (ClientError, BotoCoreError):
            return None

    @cached_property
    def regions(self) -> list[str]:
        """Regions to scan: explicit config, else every region the account opted into."""
        if self.settings.regions:
            return list(self.settings.regions)
        try:
            response = self.client("ec2", self.default_region).describe_regions(
                Filters=[{"Name": "opt-in-status", "Values": ["opt-in-not-required", "opted-in"]}]
            )
            discovered = sorted(r["RegionName"] for r in response.get("Regions", []))
            if discovered:
                return discovered
        except (ClientError, BotoCoreError) as exc:
            logger.warning("Region discovery failed (%s); using fallback list.", exc)
        return list(FALLBACK_REGIONS)

    def verify_credentials(self) -> None:
        """Fail fast with a clear message when credentials are missing or expired."""
        try:
            self.client("sts").get_caller_identity()
        except (ClientError, BotoCoreError) as exc:
            raise CredentialsUnavailable(
                "Unable to authenticate to AWS. Check your profile, SSO session, or "
                "environment credentials.\n"
                f"Underlying error: {exc}"
            ) from exc


class CredentialsUnavailable(RuntimeError):
    """Raised when the agent cannot authenticate to AWS at all."""
