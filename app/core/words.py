"""Small helpers for messages people read."""

from __future__ import annotations


def count(n: int, singular: str, plural: str | None = None) -> str:
    """``1 page``, ``4 pages``, ``0 pages``."""
    return f"{n} {singular if n == 1 else (plural or singular + 's')}"
