"""Configuration loaded from environment variables and an optional .env file.

Every setting is prefixed with ``FINOPS_``. Nested threshold settings use a double
underscore, e.g. ``FINOPS_THRESHOLDS__CPU_IDLE_PERCENT=3``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

try:  # pydantic-settings >= 2.3 lets us opt out of JSON decoding for a field.
    from pydantic_settings import NoDecode
except ImportError:  # pragma: no cover - older pydantic-settings
    NoDecode = None  # type: ignore[assignment]

_RegionList = Annotated[list[str], NoDecode] if NoDecode is not None else list[str]

LlmProvider = Literal["bedrock", "anthropic", "openai", "none"]


class Thresholds(BaseModel):
    """Tunable limits that decide when a resource is considered wasteful.

    These are deliberately conservative: a finding should be something you would
    actually act on, not every resource that is merely below peak utilization.
    """

    cpu_idle_percent: float = Field(
        5.0, description="Max average CPU% for a compute resource to count as idle."
    )
    cpu_underutilized_percent: float = Field(
        40.0, description="Max p95 CPU% below which a resource is a rightsizing candidate."
    )
    network_idle_bytes_per_day: float = Field(
        5 * 1024 * 1024, description="Max daily network bytes for an idle compute resource."
    )
    ebs_unattached_min_age_days: int = Field(
        7, description="Ignore freshly detached volumes; they are often mid-migration."
    )
    ebs_iops_overprovisioned_ratio: float = Field(
        0.30, description="Flag provisioned IOPS when observed peak stays under this fraction."
    )
    snapshot_stale_age_days: int = Field(90, description="Snapshot age before it looks orphaned.")
    ami_stale_age_days: int = Field(90, description="Unused AMI age before it looks orphaned.")
    elb_idle_requests_per_day: float = Field(
        100.0, description="Max daily requests/connections for an idle load balancer."
    )
    nat_idle_bytes_per_day: float = Field(
        1024 * 1024, description="Max daily bytes processed for an idle NAT Gateway."
    )
    rds_idle_connections: float = Field(
        1.0, description="Max average DB connections for an idle database."
    )
    lambda_memory_headroom_percent: float = Field(
        50.0, description="Flag functions using less than this share of allocated memory."
    )
    log_group_retention_max_days: int = Field(
        365, description="Retention above this (or never-expire) is flagged."
    )
    dynamodb_provisioned_utilization_percent: float = Field(
        20.0, description="Provisioned capacity below this utilization is a finding."
    )
    commitment_coverage_target_percent: float = Field(
        70.0, description="Steady-state spend below this coverage suggests a commitment gap."
    )
    min_monthly_savings_usd: float = Field(
        1.0, description="Findings worth less than this are dropped as noise."
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FINOPS_",
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # --- AWS access ---
    aws_profile: str | None = Field(
        None, description="Named AWS profile. None uses the default credential chain."
    )
    regions: _RegionList = Field(  # type: ignore[valid-type]
        default_factory=list,
        description="Regions to scan. Empty means auto-discover opted-in regions.",
    )
    billing_region: str = Field(
        "us-east-1", description="Endpoint region for Cost Explorer and Cost Optimization Hub."
    )

    # --- Analysis windows ---
    cost_lookback_days: int = Field(30, ge=1, le=365)
    metric_lookback_days: int = Field(14, ge=1, le=90)
    max_workers: int = Field(8, ge=1, le=32)

    # --- Storage ---
    db_path: Path = Field(Path("data") / "finops.db")

    # --- LLM advisor ---
    llm_provider: LlmProvider = "bedrock"
    llm_max_output_tokens: int = 4096
    llm_temperature: float = 0.2

    bedrock_model_id: str = "us.anthropic.claude-sonnet-4-20250514-v1:0"
    bedrock_region: str | None = None

    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-sonnet-4-20250514"

    openai_api_key: str | None = None
    openai_model: str = "gpt-4o"
    openai_base_url: str = "https://api.openai.com/v1"

    thresholds: Thresholds = Field(default_factory=Thresholds)

    @field_validator("regions", mode="before")
    @classmethod
    def _split_regions(cls, value: Any) -> Any:
        """Accept ``us-east-1,us-west-2`` as well as a real list."""
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @property
    def resolved_bedrock_region(self) -> str:
        return self.bedrock_region or self.billing_region


@lru_cache
def get_settings() -> Settings:
    return Settings()
