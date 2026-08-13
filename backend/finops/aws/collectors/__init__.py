"""Per-service resource collectors.

Importing this package registers every collector. Add a new service by dropping a
module here that subclasses :class:`~finops.aws.collectors.base.Collector` and applies
the ``@register`` decorator, then listing it in ``_COLLECTOR_MODULES`` below.
"""

from __future__ import annotations

from importlib import import_module

from finops.aws.collectors.base import (  # noqa: F401 - re-exported for convenience
    REGISTRY,
    CollectionContext,
    Collector,
    build_collectors,
    collect_inventory,
    register,
)

_COLLECTOR_MODULES = (
    "ec2",
    "ebs",
    "network",
    "elbv2",
    "eks",
    "rds",
    "efs",
    "s3",
    "lambda_",
    "dynamodb",
    "logs",
    "security",
    "messaging",
)

for _module in _COLLECTOR_MODULES:
    import_module(f"finops.aws.collectors.{_module}")

__all__ = [
    "REGISTRY",
    "CollectionContext",
    "Collector",
    "build_collectors",
    "collect_inventory",
    "register",
]
