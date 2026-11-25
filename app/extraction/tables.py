"""Declarative browser table captures, shared by runtime and offline extraction."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any


def source_key(source: dict) -> str:
    return hashlib.sha256(json.dumps(source, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def requested_sources(mapping: dict | None) -> list[dict]:
    return [source for field in (mapping or {}).get("fields", []) for source in field.get("sources", []) if source.get("kind") == "browser_table"]


def table_field_names(mapping: dict | None) -> list[str]:
    return [field["name"] for field in (mapping or {}).get("fields", [])
            if any(source.get("kind") == "browser_table" for source in field.get("sources", []))]


def unavailable(reason: str = "uncaptured") -> dict[str, Any]:
    return {"complete": False, "reason": reason, "pages": 0, "rows": [], "headers": [], "bytes": 0, "html": ""}


MAX_CAPTURE_BYTES = 4 * 1024 * 1024
MAX_CAPTURE_ROWS = 10000
MAX_CAPTURE_PAGES = 64
MAX_CAPTURE_SOURCES = 55  # 50 mapping fields plus five proposed sample sources.


def checked_capture(captured, source: dict | None = None) -> dict:
    """Validate the entire envelope before coverage indexes either outcome state."""
    source = ({"max_pages": 32, "max_rows": 2000, "max_bytes": 1048576} | source) if source is not None else {}
    if not isinstance(captured, dict):
        return unavailable("malformed_capture")
    valid = (
        type(captured.get("complete")) is bool
        and isinstance(captured.get("reason"), str) and 0 < len(captured["reason"]) <= 64
        and isinstance(captured.get("html"), str)
        and isinstance(captured.get("rows"), list)
        and len(captured["rows"]) <= min(MAX_CAPTURE_ROWS, int(source.get("max_rows", MAX_CAPTURE_ROWS)))
        and all(isinstance(row, list) and len(row) <= 64 and all(isinstance(cell, str) for cell in row) for row in captured["rows"])
        and isinstance(captured.get("headers"), list) and len(captured["headers"]) <= 64
        and all(isinstance(header, str) for header in captured["headers"])
        and type(captured.get("pages")) is int and 0 <= captured["pages"] <= min(MAX_CAPTURE_PAGES, int(source.get("max_pages", MAX_CAPTURE_PAGES)))
        and type(captured.get("bytes")) is int and 0 <= captured["bytes"] <= min(MAX_CAPTURE_BYTES, int(source.get("max_bytes", MAX_CAPTURE_BYTES)))
        and len(captured["html"].encode("utf-8")) <= min(MAX_CAPTURE_BYTES, int(source.get("max_bytes", MAX_CAPTURE_BYTES)))
    )
    if not valid or (captured["complete"] and (captured["reason"] != "finished" or captured["pages"] < 1 or not captured["html"])):
        return unavailable("malformed_capture")
    # Captures are a closed envelope: unknown large private properties must not
    # escape the acquisition and persistence accounting.
    return {key: captured[key] for key in unavailable()}


def capture_for(page, source: dict) -> dict:
    captures = getattr(page, "table_captures", {}) or {}
    if not isinstance(captures, dict):
        return unavailable("malformed_capture")
    captured = captures.get(source_key(source))
    if captured is None:
        return unavailable()
    defaults = {"max_pages": 32, "max_rows": 2000, "max_bytes": 1048576}
    return checked_capture(captured, defaults | source)


def capture_bytes(captures: dict) -> int:
    return len(json.dumps(captures, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


class TableCaptureBudget:
    """One page's shared evidence budget, including existing captures and outcomes."""

    def __init__(self, captures: dict, sources: list[dict], *, max_bytes=MAX_CAPTURE_BYTES,
                 max_rows=MAX_CAPTURE_ROWS, max_pages=MAX_CAPTURE_PAGES):
        self.captures = captures
        self.max_bytes = min(MAX_CAPTURE_BYTES, max_bytes)
        self.max_rows = min(MAX_CAPTURE_ROWS, max_rows)
        self.max_pages = min(MAX_CAPTURE_PAGES, max_pages)
        original = dict(captures)
        self.sources = {source_key(source): source for source in sources}
        keys = list(dict.fromkeys([*original, *self.sources]))
        if len(keys) > MAX_CAPTURE_SOURCES:
            raise ValueError("too many table capture sources")
        self.pending = set(self.sources) - set(original)
        captures.clear()
        # Allocate small, well-formed outcomes up front, so later exhausted
        # sources remain explicit without exceeding the total serialized cap.
        captures.update({key: unavailable("aggregate_byte_limit") for key in keys})
        if capture_bytes(captures) > self.max_bytes:
            raise ValueError("table evidence budget cannot hold source outcomes")
        for key, captured in original.items():
            self._retain(key, checked_capture(captured, self.sources.get(key)))

    def needs_capture(self, source: dict) -> bool:
        return source_key(source) in self.pending

    def bounds(self, source: dict) -> tuple[dict | None, str | None]:
        key = source_key(source)
        rows = self.max_rows - sum(len(value["rows"]) for name, value in self.captures.items() if name != key)
        pages = self.max_pages - sum(value["pages"] for name, value in self.captures.items() if name != key)
        if rows <= 0:
            return None, "aggregate_row_limit"
        if pages <= 0:
            return None, "aggregate_page_limit"
        # The collector counts row strings/fragments before browser IPC. Reserve
        # the outcome envelope and serialization overhead, then independently
        # verify the exact retained JSON bytes in _retain.
        remaining = self.max_bytes - capture_bytes(self.captures) - 1024
        if remaining < 128:  # Reserve enough room for a bounded DOM envelope.
            return None, "aggregate_byte_limit"
        return source | {
            "max_bytes": min(int(source.get("max_bytes", 1048576)), remaining),
            "max_rows": min(int(source.get("max_rows", 2000)), rows),
            "max_pages": min(int(source.get("max_pages", 32)), pages),
        }, None

    def _retain(self, key: str, captured: dict) -> None:
        previous = self.captures[key]
        others = [value for name, value in self.captures.items() if name != key]
        if sum(len(value["rows"]) for value in others) + len(captured["rows"]) > self.max_rows:
            captured = unavailable("aggregate_row_limit")
        elif sum(value["pages"] for value in others) + captured["pages"] > self.max_pages:
            captured = unavailable("aggregate_page_limit")
        self.captures[key] = captured
        if capture_bytes(self.captures) > self.max_bytes:
            self.captures[key] = unavailable("aggregate_byte_limit")
            # The initial reserved placeholder bounds this replacement too.
            if capture_bytes(self.captures) > self.max_bytes:
                self.captures[key] = previous
        self.pending.discard(key)

    def retain(self, source: dict, captured: dict, bounded: dict | None = None) -> None:
        key = source_key(source)
        captured = checked_capture(captured, source)
        if bounded and not captured["complete"]:
            for limit, default in (("bytes", 1048576), ("rows", 2000), ("pages", 32)):
                reason = {"bytes": "byte_limit", "rows": "row_limit", "pages": "page_limit"}[limit]
                if captured["reason"] == reason and bounded["max_" + limit] < source.get("max_" + limit, default):
                    captured = captured | {"reason": "aggregate_" + reason}
        self._retain(key, captured)


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


def restore_captures(page, saved) -> None:
    """Reapply page evidence limits before stored samples/pages can be replayed."""
    page.table_captures = {}
    if isinstance(saved, dict):
        for key, value in saved.items():
            if isinstance(key, str) and re.fullmatch(r"[0-9a-f]{64}", key):
                page.table_captures[key] = value
                if len(page.table_captures) >= MAX_CAPTURE_SOURCES:
                    break
    TableCaptureBudget(page.table_captures, [])


def referenced_captures(data_dir, receipt: dict, *, run_id=None) -> dict:
    """Read a bounded private KVS reference, or retain an explicit replay gap."""
    from pathlib import Path

    receipt = receipt if isinstance(receipt, dict) else {}
    key = receipt.get("key")
    run_id = run_id or receipt.get("run_id")
    if (receipt.get("retained") is True and data_dir is not None
            and isinstance(key, str) and re.fullmatch(r"table_evidence_[a-f0-9]{64}", key)
            and isinstance(run_id, str) and re.fullmatch(r"run_[A-Z0-9]{26}", run_id)):
        directory = Path(data_dir) / "jobs" / run_id / "crawlee" / "key_value_stores" / "default"
        # Exact names: prefix globs could pick up the reservation marker.
        for path in (directory / key, directory / (key + ".json")):
            try:
                if path.stat().st_size <= MAX_CAPTURE_BYTES:
                    captured = json.loads(path.read_text("utf-8"))
                    if isinstance(captured, dict):
                        return captured
            except (OSError, ValueError):
                pass
    reason = "storage_limit" if receipt.get("reason") == "storage_limit" else "evidence_unavailable"
    return {source: unavailable(reason) for source in (receipt.get("source_keys") or [])[:MAX_CAPTURE_SOURCES]
            if isinstance(source, str) and re.fullmatch(r"[a-f0-9]{64}", source)}
