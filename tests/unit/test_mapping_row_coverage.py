"""Mapping quality measures emitted rows, including variant-only field sources."""

import json
from types import SimpleNamespace

import pytest

from app.core.db import Database
from app.extraction.samples_store import SampleStore
from app.settings import LIMIT_DEFAULTS
from app.setup_ops import mapping_test


@pytest.fixture
def sample_service(tmp_path):
    database = Database(tmp_path / "service.sqlite3")
    samples = SampleStore(database, tmp_path)
    service = SimpleNamespace(
        samples=samples,
        settings=SimpleNamespace(
            limits={name: limits[0] for name, limits in LIMIT_DEFAULTS.items()}
        ),
    )
    yield service
    database.close()


def _mapping():
    return {
        "schema_version": 1,
        "template_id": "entry",
        "match": {"selector": "h1"},
        "fields": [
            {
                "name": "title",
                "type": "text",
                "required": True,
                "sources": [{"kind": "css", "selector": "h1", "extract": "text"}],
            },
            {
                "name": "description",
                "type": "text",
                "required": True,
                "sources": [
                    {
                        "kind": "css",
                        "selector": ".absent-parent-description",
                        "extract": "text",
                    }
                ],
            },
        ],
        "variants": {
            "sources": [
                {
                    "kind": "embedded_json",
                    "selector": "script#state",
                    "path": "choices",
                    "format": "generic",
                }
            ],
            "fields": [
                {
                    "name": "allowed",
                    "path": "raw.enabled",
                    "type": "boolean",
                    "fallback_to_page": False,
                },
                {
                    "name": "description",
                    "selector_template": '.notes[data-choice="{id}"]',
                    "extract": "text",
                    "type": "text",
                    "fallback_to_page": False,
                },
            ],
        },
    }


def _save(service, sample_id, choices, descriptions):
    html = (
        '<h1>Shared title</h1><script type="application/json" id="state">'
        + json.dumps({"choices": choices})
        + "</script>"
    )
    html += "".join(
        f'<section class="notes" data-choice="{identity}">{text}</section>'
        for identity, text in descriptions.items()
    )
    service.samples.save(
        {
            "sample_id": sample_id,
            "url": "https://example.test/" + sample_id,
            "expires_at": "2030-01-01T00:00:00Z",
            "variant_sources": [],
        },
        html,
        "https://example.test/" + sample_id,
    )


async def test_required_variant_dom_field_is_measured_on_each_emitted_row(
    sample_service,
):
    _save(
        sample_service,
        "smp_one",
        [{"id": "first", "enabled": True}, {"id": "second", "enabled": True}],
        {"first": "First edition notes", "second": "Second edition notes"},
    )
    result = await mapping_test(
        sample_service,
        {
            "mappings": [_mapping()],
            "sample_ids": ["smp_one"],
            "required_fill_threshold": 0.9,
        },
    )
    assert result["passed"] is True
    description = next(
        field
        for field in result["templates"][0]["fields"]
        if field["name"] == "description"
    )
    assert (
        description["sample_count"],
        description["filled_count"],
        description["fill_rate"],
    ) == (2, 2, 1.0)
    assert set(description["example_values"]) == {
        "First edition notes",
        "Second edition notes",
    }


async def test_excluded_rows_do_not_reduce_required_field_fill_rate(sample_service):
    _save(
        sample_service,
        "smp_one",
        [{"id": "first", "enabled": True}, {"id": "unavailable", "enabled": False}],
        {"first": "Selected notes"},
    )
    _save(sample_service, "smp_two", [{"id": "excluded", "enabled": False}], {})
    mapping = _mapping()
    mapping["record_filter"] = [{"field": "allowed", "op": "eq", "value": True}]
    result = await mapping_test(
        sample_service,
        {
            "mappings": [mapping],
            "sample_ids": ["smp_one", "smp_two"],
            "required_fill_threshold": 0.9,
        },
    )
    assert result["passed"] is True
    description = next(
        field
        for field in result["templates"][0]["fields"]
        if field["name"] == "description"
    )
    assert (
        description["sample_count"],
        description["filled_count"],
        description["fill_rate"],
    ) == (1, 1, 1.0)
    assert result["templates"][0]["matched_samples"] == 2
    assert all(not sample["missing_required"] for sample in result["samples"])


async def test_required_variant_only_field_uses_selected_row_values(sample_service):
    _save(
        sample_service,
        "smp_one",
        [{"id": "first", "sku": "EDITION-A", "enabled": True}],
        {"first": "First edition notes"},
    )
    mapping = _mapping()
    mapping["variants"]["fields"].append({"name": "sku", "path": "sku"})
    result = await mapping_test(
        sample_service,
        {
            "mappings": [mapping],
            "sample_ids": ["smp_one"],
            "required_fields": ["sku"],
            "required_fill_threshold": 0.9,
        },
    )
    assert result["passed"] is True
    assert result["samples"][0]["missing_required"] == []
    sku = next(
        field for field in result["templates"][0]["fields"] if field["name"] == "sku"
    )
    assert (
        sku["sample_count"],
        sku["filled_count"],
        sku["fill_rate"],
        sku["passed"],
    ) == (1, 1, 1.0, True)
    assert sku["example_values"] == ["EDITION-A"]


async def test_required_undeclared_field_still_fails_mapping_preview(sample_service):
    _save(
        sample_service,
        "smp_one",
        [{"id": "first", "enabled": True}],
        {"first": "First edition notes"},
    )
    result = await mapping_test(
        sample_service,
        {
            "mappings": [_mapping()],
            "sample_ids": ["smp_one"],
            "required_fields": ["undeclared"],
            "required_fill_threshold": 0.9,
        },
    )
    assert result["passed"] is False
    assert result["samples"][0]["missing_required"] == ["undeclared"]


def _nested_mapping():
    mapping = _mapping()
    mapping["fields"] = mapping["fields"][:1]
    mapping["variants"] = {
        "sources": [
            {
                "kind": "embedded_json",
                "selector": "script#state",
                "path": "choices",
                "children_path": "packages",
                "format": "generic",
            }
        ],
        "fields": [
            {"name": "year", "path": "parent.raw.year", "fallback_to_page": False},
            {"name": "sku", "path": "raw.sku", "fallback_to_page": False},
            {
                "name": "available",
                "path": "raw.available",
                "type": "boolean",
                "fallback_to_page": False,
            },
        ],
    }
    return mapping


async def test_nested_mapping_preview_keeps_sold_out_and_unknown_stock_rows(
    sample_service,
):
    _save(
        sample_service,
        "smp_nested",
        [
            {
                "year": "2023",
                "packages": [
                    {"sku": "001", "available": False},
                    {"sku": "002", "available": True},
                ],
            },
            {"year": "2024", "packages": [{"sku": "003"}]},
        ],
        {},
    )
    result = await mapping_test(
        sample_service,
        {
            "mappings": [_nested_mapping()],
            "sample_ids": ["smp_nested"],
            "required_fields": ["title", "sku", "year"],
            "required_fill_threshold": 1.0,
        },
    )
    assert result["passed"] is True
    preview = result["samples"][0]["variants"]
    assert preview["count"] == 3
    assert [
        (item["data"]["year"], item["data"]["sku"], item["data"]["available"])
        for item in preview["items"]
    ] == [
        ("2023", "001", False),
        ("2023", "002", True),
        ("2024", "003", None),
    ]


async def test_nested_parent_without_children_still_fails_required_package_sku(
    sample_service,
):
    _save(
        sample_service, "smp_missing_children", [{"year": "2025", "packages": []}], {}
    )
    result = await mapping_test(
        sample_service,
        {
            "mappings": [_nested_mapping()],
            "sample_ids": ["smp_missing_children"],
            "required_fields": ["title", "sku", "year"],
            "required_fill_threshold": 1.0,
        },
    )
    assert result["passed"] is False
    assert result["samples"][0]["missing_required"] == ["sku"]


async def test_unknown_filter_value_fails_preview_when_variant_source_is_missing(
    sample_service,
):
    _save(sample_service, "smp_one", [], {})
    mapping = _mapping()
    mapping["record_filter"] = [{"field": "allowed", "op": "eq", "value": True}]
    result = await mapping_test(
        sample_service,
        {
            "mappings": [mapping],
            "sample_ids": ["smp_one"],
            "required_fill_threshold": 0.9,
        },
    )
    assert result["passed"] is False
    assert result["samples"][0]["missing_required"] == ["allowed"]
