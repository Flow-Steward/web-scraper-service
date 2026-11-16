"""RFC 3339 UTC timestamps with a ``Z`` suffix."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def now() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    value = value.astimezone(UTC)
    if value.microsecond:
        return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    return iso(now())  # type: ignore[return-value]


def parse(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def plus(seconds: float, base: datetime | None = None) -> datetime:
    return (base or now()) + timedelta(seconds=seconds)
