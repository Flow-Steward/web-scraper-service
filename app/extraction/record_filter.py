"""Fixed declarative predicates over typed record data, never expressions."""

from __future__ import annotations

import math
from typing import Any


def _number(value: Any) -> bool:
    return type(value) is int or (isinstance(value, float) and math.isfinite(value))


def record_filter_missing(mapping: dict, data: dict) -> list[str]:
    """Unknown inclusion inputs need diagnostics, rather than a silent exclusion."""
    return sorted(
        {
            condition["field"]
            for condition in mapping.get("record_filter") or []
            if data.get(condition["field"]) is None
            or data.get(condition["field"]) == []
        }
    )


def record_matches(mapping: dict, data: dict) -> bool:
    """All predicates must match; missing/null and incompatible values fail closed."""
    for condition in mapping.get("record_filter") or []:
        actual = data.get(condition["field"])
        expected = condition["value"]
        if actual is None or expected is None:
            return False
        if condition["op"] == "eq":
            same_type = type(actual) is type(expected) or (
                _number(actual) and _number(expected)
            )
            if not same_type or actual != expected:
                return False
        elif condition["op"] == "gt":
            if not (_number(actual) and _number(expected)) or actual <= expected:
                return False
        else:
            return False
    return True
