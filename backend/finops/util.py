"""Small shared helpers used across the collectors, rules, and reporting layers."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from datetime import UTC, datetime
from typing import Any, TypeVar

from dateutil import parser as date_parser

T = TypeVar("T")


def parse_aws_timestamp(value: Any) -> datetime | None:
    """Parse the assorted timestamp shapes AWS returns (datetime, ISO string, epoch)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC)
    try:
        parsed = date_parser.parse(str(value))
    except (ValueError, OverflowError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def days_since(value: Any, *, now: datetime | None = None) -> float | None:
    """Age of a timestamp in days, or None when it cannot be determined."""
    parsed = parse_aws_timestamp(value)
    if parsed is None:
        return None
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    return (reference - parsed).total_seconds() / 86400.0


def safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    return numerator / denominator if denominator else default


def percent(part: float, whole: float) -> float:
    return round(safe_div(part, whole) * 100.0, 2)


def round_money(value: float | None) -> float:
    return round(value or 0.0, 2)


def chunked(items: Sequence[T], size: int) -> Iterator[list[T]]:
    """Split a sequence into fixed-size batches (for APIs with per-call limits)."""
    if size < 1:
        raise ValueError("size must be >= 1")
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def human_money(value: float | None, currency: str = "USD") -> str:
    amount = value or 0.0
    symbol = "$" if currency == "USD" else f"{currency} "
    if abs(amount) >= 1_000_000:
        return f"{symbol}{amount / 1_000_000:.2f}M"
    if abs(amount) >= 10_000:
        return f"{symbol}{amount / 1_000:.1f}k"
    return f"{symbol}{amount:,.2f}"


def top_n(mapping: dict[str, float], n: int, *, reverse: bool = True) -> list[tuple[str, float]]:
    return sorted(mapping.items(), key=lambda kv: kv[1], reverse=reverse)[:n]


def first_tag(tags: dict[str, str], *keys: str) -> str | None:
    """Return the first present tag value, matching keys case-insensitively."""
    lowered = {k.lower(): v for k, v in tags.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value:
            return value
    return None


def dedupe_preserving_order(items: Iterable[T]) -> list[T]:
    seen: set[T] = set()
    result: list[T] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result
