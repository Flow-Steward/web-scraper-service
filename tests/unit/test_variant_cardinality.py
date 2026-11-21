"""Per-variant scalars retain the parent field's declared cardinality."""

import json
from types import SimpleNamespace

import pytest

from app.core.csvout import record_columns, record_row
from app.core.db import Database
from app.core.schemas import validator
from app.extraction import variants as V
from app.extraction.samples_store import SampleStore
from app.settings import LIMIT_DEFAULTS
from app.setup_ops import mapping_test

BASE = "https://example.test/item"


def _mapping(field_type="image_url", multiple=True):
    return {
        "schema_version": 1,
        "template_id": "entry",
        "match": {"selector": "h1"},
        "fields": [
            {
                "name": "value",
                "type": field_type,
                "multiple": multiple,
                "required": True,
                "sources": [
                    {
                        "kind": "css",
                        "selector": ".missing-page-value",
                        "extract": "text",
                    }
                ],
            }
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
                {"name": "value", "path": "raw.value", "fallback_to_page": False}
            ],
        },
    }


@pytest.mark.parametrize(
    ("field_type", "raw_value", "expected"),
    [
        ("image_url", "/edition.jpg", ["https://example.test/edition.jpg"]),
        ("boolean", False, [False]),
        ("number", 0, [0.0]),
        ("text", "Edition A", ["Edition A"]),
    ],
)
def test_variant_scalar_override_preserves_parent_multiple(
    field_type, raw_value, expected
):
    mapping = _mapping(field_type)
    assert not list(validator("mapping.schema.json").iter_errors(mapping))
    data, filled = V.apply_variant(
        mapping, {"value": []}, V.from_generic({"value": raw_value}), BASE
    )
    assert data["value"] == expected
    assert filled["value"] is True


@pytest.mark.parametrize("fallback_to_page", [False, True])
def test_missing_variant_value_keeps_null_or_existing_page_array(fallback_to_page):
    mapping = _mapping()
    mapping["variants"]["fields"][0]["fallback_to_page"] = fallback_to_page
    parent = {"value": ["https://example.test/parent.jpg"]}
    data, filled = V.apply_variant(mapping, parent, V.from_generic({}), BASE)
    assert data["value"] == (parent["value"] if fallback_to_page else None)
    assert filled["value"] is False
    absent = V.variant_rows(mapping, parent, [], None, BASE)
    assert absent[0][0]["value"] == (parent["value"] if fallback_to_page else None)


def test_single_parent_field_remains_scalar_and_variant_multiple_key_is_rejected():
    mapping = _mapping("text", multiple=False)
    data, _ = V.apply_variant(
        mapping, {"value": "parent"}, V.from_generic({"value": "edition"}), BASE
    )
    assert data["value"] == "edition"
    mapping["variants"]["fields"][0]["multiple"] = True
    assert list(validator("mapping.schema.json").iter_errors(mapping))


async def test_preview_required_record_and_csv_consumers_keep_multiple_variant_values(
    tmp_path,
):
    database = Database(tmp_path / "service.sqlite3")
    try:
        samples = SampleStore(database, tmp_path)
        service = SimpleNamespace(
            samples=samples,
            settings=SimpleNamespace(
                limits={key: value[0] for key, value in LIMIT_DEFAULTS.items()}
            ),
        )
        samples.save(
            {
                "sample_id": "smp_one",
                "url": BASE,
                "expires_at": "2030-01-01T00:00:00Z",
                "variant_sources": [],
            },
            '<h1>Item</h1><script type="application/json" id="state">'
            + json.dumps({"choices": [{"value": "/edition.jpg"}]})
            + "</script>",
            BASE,
        )
        preview = await mapping_test(
            service,
            {
                "mappings": [_mapping()],
                "sample_ids": ["smp_one"],
                "required_fill_threshold": 0.9,
            },
        )
        assert preview["passed"] is True
        assert preview["samples"][0]["missing_required"] == []
        data = preview["samples"][0]["variants"]["items"][0]["data"]
        assert data["value"] == ["https://example.test/edition.jpg"]
        record = {
            "record_id": "rec_" + "0" * 32,
            "source_url": BASE,
            "site": "example.test",
            "template_id": "entry",
            "mapping_version": 1,
            "item_index": 0,
            "extracted_at": "2026-11-20T00:00:00Z",
            "data": data,
        }
        assert not list(validator("record.schema.json").iter_errors(record))
        columns = record_columns([record])
        assert (
            dict(zip(columns, record_row(record, columns)))["data.value"]
            == "https://example.test/edition.jpg"
        )
    finally:
        database.close()


@pytest.mark.parametrize(
    ("field_type", "raw_value", "transforms", "expected"),
    [
        (
            "text",
            [" first ", None, {}, ["nested"], " second ", ""],
            [{"op": "trim"}],
            ["first", "second"],
        ),
        ("boolean", [False, None, {}, True], [], [False, True]),
        ("number", [0, None, {}, 3], [], [0.0, 3.0]),
        (
            "number",
            ["value:0", "invalid", "value:2"],
            [{"op": "regex_replace", "pattern": "^value:", "replacement": ""}],
            [0.0, 2.0],
        ),
        (
            "image_url",
            ["/first.jpg", None, {}, ["/nested.jpg"], "/second.jpg"],
            [],
            ["https://example.test/first.jpg", "https://example.test/second.jpg"],
        ),
    ],
)
def test_variant_primitive_list_preserves_parent_multiple_and_coerces_each_item(
    field_type, raw_value, transforms, expected
):
    mapping = _mapping(field_type)
    mapping["variants"]["fields"][0]["transforms"] = transforms
    assert not list(validator("mapping.schema.json").iter_errors(mapping))

    data, filled = V.apply_variant(
        mapping,
        {"value": ["parent"]},
        V.from_generic({"value": raw_value}),
        BASE,
    )

    assert data["value"] == expected
    assert filled["value"] is True


@pytest.mark.parametrize("fallback_to_page", [False, True])
@pytest.mark.parametrize("raw_value", [None, [None, {}, ["nested"], " "]])
def test_invalid_variant_list_items_use_authoritative_missing_behavior(
    fallback_to_page, raw_value
):
    mapping = _mapping("text")
    mapping["variants"]["fields"][0]["fallback_to_page"] = fallback_to_page
    parent = {"value": ["parent"]}

    data, filled = V.apply_variant(
        mapping, parent, V.from_generic({"value": raw_value}), BASE
    )

    assert data["value"] == (parent["value"] if fallback_to_page else None)
    assert filled["value"] is False


@pytest.mark.parametrize("fallback_to_page", [False, True])
def test_empty_variant_list_keeps_empty_authoritative_array_or_page_fallback(
    fallback_to_page,
):
    mapping = _mapping("text")
    mapping["variants"]["fields"][0]["fallback_to_page"] = fallback_to_page
    parent = {"value": ["parent"]}

    data, filled = V.apply_variant(mapping, parent, V.from_generic({"value": []}), BASE)

    assert data["value"] == (parent["value"] if fallback_to_page else [])
    assert filled["value"] is False


def test_variant_list_is_not_stringified_for_single_parent_field():
    mapping = _mapping("text", multiple=False)

    data, filled = V.apply_variant(
        mapping,
        {"value": "parent"},
        V.from_generic({"value": ["first", "second"]}),
        BASE,
    )

    assert data["value"] is None
    assert filled["value"] is False
