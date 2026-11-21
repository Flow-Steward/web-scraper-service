"""Variant overrides share the bounded field capacity of their parent mapping."""

from __future__ import annotations

import pytest

from app.core.schemas import validator


def _mapping(*, parent_count: int = 50, variant_count: int = 22) -> dict:
    return {
        "schema_version": 1,
        "template_id": "entry",
        "match": {"selector": "h1"},
        "fields": [
            {
                "name": f"attribute_{index}",
                "type": "text",
                "sources": [{"kind": "css", "selector": "h1", "extract": "text"}],
            }
            for index in range(parent_count)
        ],
        "variants": {
            "sources": [{"kind": "embedded_json", "selector": "script#state"}],
            "fields": [
                {
                    "name": f"attribute_{index}",
                    "path": f"raw.attribute_{index}",
                    "fallback_to_page": False,
                }
                for index in range(variant_count)
            ],
        },
    }


@pytest.mark.parametrize("variant_count", [22, 50])
def test_mapping_accepts_independent_variant_fields_up_to_parent_capacity(
    variant_count,
):
    mapping = _mapping(variant_count=variant_count)

    errors = list(validator("mapping.schema.json").iter_errors(mapping))

    assert not errors, [error.message for error in errors]


def test_mapping_rejects_fifty_one_variant_fields():
    errors = list(
        validator("mapping.schema.json").iter_errors(_mapping(variant_count=51))
    )

    assert any(
        list(error.absolute_path) == ["variants", "fields"]
        and error.validator == "maxItems"
        for error in errors
    ), [error.message for error in errors]


def test_parent_mapping_still_rejects_fifty_one_fields():
    errors = list(
        validator("mapping.schema.json").iter_errors(_mapping(parent_count=51))
    )

    assert any(
        list(error.absolute_path) == ["fields"] and error.validator == "maxItems"
        for error in errors
    ), [error.message for error in errors]
