"""Deterministic cost reduction rules.

Importing this package registers every rule. Add a new one by dropping a module here
that subclasses :class:`~finops.rules.base.Rule` with the ``@register`` decorator, then
listing the module in ``_RULE_MODULES``.
"""

from __future__ import annotations

from importlib import import_module

from finops.rules.base import (  # noqa: F401 - re-exported for convenience
    REGISTRY,
    Rule,
    RuleContext,
    build_rules,
    finding_for,
    merge_findings,
    register,
    run_rules,
)

_RULE_MODULES = (
    "idle",
    "rightsizing",
    "storage",
    "network",
    "containers",
    "database",
    "commitments",
    "governance",
)

for _module in _RULE_MODULES:
    import_module(f"finops.rules.{_module}")

__all__ = [
    "REGISTRY",
    "Rule",
    "RuleContext",
    "build_rules",
    "finding_for",
    "merge_findings",
    "register",
    "run_rules",
]
