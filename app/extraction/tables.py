"""Declarative browser table captures, shared by runtime and offline extraction."""
from __future__ import annotations

import hashlib
import json
from typing import Any


def source_key(source: dict) -> str:
    return hashlib.sha256(json.dumps(source, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def requested_sources(mapping: dict | None) -> list[dict]:
    return [source for field in (mapping or {}).get("fields", []) for source in field.get("sources", []) if source.get("kind") == "browser_table"]


def unavailable(reason: str = "uncaptured") -> dict[str, Any]:
    return {"complete": False, "reason": reason, "pages": 0, "rows": [], "headers": [], "bytes": 0, "html": ""}


def capture_for(page, source: dict) -> dict:
    captured = (getattr(page, "table_captures", {}) or {}).get(source_key(source))
    if captured is None:
        return unavailable()
    if not isinstance(captured, dict):
        return unavailable("malformed_capture")
    if captured.get("complete") is True:
        valid = (
            captured.get("reason") == "finished"
            and isinstance(captured.get("html"), str) and bool(captured["html"])
            and isinstance(captured.get("rows"), list)
            and all(isinstance(row, list) and all(isinstance(cell, str) for cell in row) for row in captured["rows"])
            and isinstance(captured.get("headers"), list)
            and all(isinstance(header, str) for header in captured["headers"])
            and type(captured.get("pages")) is int and 1 <= captured["pages"] <= int(source.get("max_pages", 32))
            and len(captured["rows"]) <= int(source.get("max_rows", 2000))
            and type(captured.get("bytes")) is int and 0 <= captured["bytes"] <= int(source.get("max_bytes", 1048576))
            and len(captured["html"].encode("utf-8")) <= int(source.get("max_bytes", 1048576))
        )
        if not valid:
            return unavailable("malformed_capture")
    elif captured.get("complete") is not False:
        return unavailable("malformed_capture")
    return captured


def table_coverage(mapping: dict, page) -> dict[str, dict]:
    coverage = {}
    for spec in mapping.get("fields", []):
        for source in spec.get("sources", []):
            if source.get("kind") == "browser_table":
                captured = capture_for(page, source)
                coverage[spec["name"]] = {
                    "complete": captured["complete"], "reason": captured["reason"],
                    "pages": captured["pages"], "bytes": captured["bytes"],
                    "row_count": len(captured["rows"]),
                }
    return coverage


def table_failures(mapping: dict, page) -> dict[str, dict]:
    return {name: info for name, info in table_coverage(mapping, page).items() if not info.get("complete")}
