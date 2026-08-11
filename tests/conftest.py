from __future__ import annotations

import os

import pytest

from finops.aws.collectors.base import CollectionContext
from finops.aws.session import AwsContext
from finops.config import Settings

TEST_REGION = "us-east-1"


@pytest.fixture(autouse=True)
def aws_credentials(monkeypatch):
    """Point boto3 at fake credentials so a stray call can never reach real AWS."""
    for key, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": TEST_REGION,
    }.items():
        monkeypatch.setenv(key, value)
    # Ignore any FINOPS_* settings the developer has exported locally, and the .env file
    # too: a real API key sitting there must not decide whether an assertion holds.
    for key in list(os.environ):
        if key.startswith("FINOPS_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)


@pytest.fixture
def anyio_backend() -> str:
    """The chat agent and MCP hub are async; asyncio is the only backend we ship on."""
    return "asyncio"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        regions=[TEST_REGION],
        billing_region=TEST_REGION,
        db_path=tmp_path / "finops.db",
        max_workers=2,
        llm_provider="none",
        _env_file=None,
    )


@pytest.fixture
def collection_context(settings) -> CollectionContext:
    return CollectionContext(aws=AwsContext(settings=settings))
