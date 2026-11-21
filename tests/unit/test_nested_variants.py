"""Nested choices retain relational context; no site-specific keys or values."""

import json

import pytest

from app.core.schemas import validator
from app.extraction import variants as V
from app.extraction.page import Page


def page(state):
    return Page(
        '<script id="state" type="application/json">' + json.dumps(state) + "</script>",
        "https://example.test/product",
    )


def source(**changes):
    return {
        "kind": "embedded_json",
        "selector": "script#state",
        "path": "editions",
        "children_path": "packages",
        "format": "generic",
        **changes,
    }


def editions():
    return [
        {
            "year": "2023",
            "notes": "Earlier edition",
            "packages": [
                {"id": "small", "sku": "A-23", "size": "single", "available": False},
                {"id": "large", "sku": "B-23", "size": "case", "available": True},
            ],
        },
        {
            "year": "2024",
            "notes": "Later edition",
            "packages": [{"id": "small", "sku": "A-24", "size": "single"}],
        },
    ]


def test_each_child_keeps_its_own_parent_and_authoritative_optional_fields():
    parents = editions()
    rows = V.from_embedded(page({"editions": parents}), source())
    mapping = {
        "fields": [],
        "variants": {
            "fields": [
                {"name": "year", "path": "parent.raw.year"},
                {"name": "notes", "path": "parent.raw.notes"},
                {"name": "sku", "path": "raw.sku"},
                {"name": "size", "path": "raw.size"},
                {
                    "name": "available",
                    "path": "raw.available",
                    "type": "boolean",
                    "fallback_to_page": False,
                },
            ]
        },
    }
    data = [
        V.apply_variant(mapping, {"available": True}, row, "https://example.test/")[0]
        for row in rows
    ]
    assert data == [
        {
            "year": "2023",
            "notes": "Earlier edition",
            "sku": "A-23",
            "size": "single",
            "available": False,
        },
        {
            "year": "2023",
            "notes": "Earlier edition",
            "sku": "B-23",
            "size": "case",
            "available": True,
        },
        {
            "year": "2024",
            "notes": "Later edition",
            "sku": "A-24",
            "size": "single",
            "available": None,
        },
    ]
    assert [row["id"] for row in rows] == ["small", "large", "small"]
    assert rows[0]["parent"]["raw"] == parents[0]
    assert rows[2]["parent"]["raw"] == parents[1]


@pytest.mark.parametrize(
    "children", [{}, {"packages": []}, {"packages": None}, {"packages": "invalid"}]
)
def test_missing_children_keep_parent_context_without_inventing_a_package(children):
    rows = V.from_embedded(page({"editions": [{"year": "2025", **children}]}), source())
    assert len(rows) == 1
    assert rows[0]["parent"]["raw"]["year"] == "2025"
    assert rows[0]["raw"] == {}
    data, filled = V.apply_variant(
        {
            "fields": [],
            "variants": {
                "fields": [
                    {"name": "sku", "path": "raw.sku", "fallback_to_page": False}
                ]
            },
        },
        {"sku": "PAGE-SKU"},
        rows[0],
        "https://example.test/",
    )
    assert data["sku"] is None
    assert filled["sku"] is False


def test_child_path_is_relative_to_each_parent_and_can_be_dotted():
    state = [
        {"year": "first", "inventory": {"choices": [{"sku": "001"}]}},
        {"year": "second", "inventory": {"choices": [{"sku": "002"}]}},
    ]
    src = source(children_path="inventory.choices")
    src.pop("path")
    rows = V.from_embedded(page(state), src)
    assert [(r["raw"]["sku"], r["parent"]["raw"]["year"]) for r in rows] == [
        ("001", "first"),
        ("002", "second"),
    ]


def test_ordinary_variant_source_does_not_acquire_parent_context():
    src = source()
    src.pop("children_path")
    rows = V.from_embedded(page({"editions": editions()}), src)
    assert len(rows) == 2
    assert all("parent" not in row for row in rows)
    assert [row["raw"]["year"] for row in rows] == ["2023", "2024"]


def test_nested_candidate_covers_every_parent_instead_of_a_fixed_first_index():
    p = page({"editions": editions()})
    candidates = V.detect_candidates(p)
    candidate = next(
        c for c in candidates if c["source"].get("children_path") == "packages"
    )
    assert candidate["source"]["path"] == "editions"
    assert candidate["count"] == 3
    rows = V.from_embedded(p, candidate["source"])
    assert [r["raw"]["sku"] for r in rows] == ["A-23", "B-23", "A-24"]
    assert candidate["parent_examples"][0]["year"] == "2023"
    assert "parent.raw.year" in candidate["context_paths"]
    assert len(candidates) <= 12
    assert len(json.dumps(candidates, ensure_ascii=False)) <= 32768


def test_nested_candidate_prioritizes_parent_context_over_child_detail_paths():
    technical = {f"detail{i}": i for i in range(35)}
    p = page(
        {"editions": [{"packages": [technical], "year": "2025", "enabled": False}]}
    )
    candidate = next(
        c for c in V.detect_candidates(p) if c["source"].get("children_path")
    )
    assert "parent.raw.year" in candidate["context_paths"]
    assert "parent.raw.enabled" in candidate["context_paths"]


def test_candidate_sources_with_long_child_keys_still_satisfy_the_mapping_contract():
    p = page({"editions": [{"year": "2025", "x" * 301: [{"sku": "one"}]}]})
    candidates = V.detect_candidates(p)
    assert candidates
    for candidate in candidates:
        assert not list(
            validator("mapping.schema.json").iter_errors(mapping(candidate["source"]))
        )


def test_nested_candidate_finds_children_after_empty_early_parents():
    p = page(
        {
            "editions": [
                {"year": "2021", "packages": []},
                {"year": "2022", "packages": []},
                {"year": "2023", "packages": [{"sku": "003"}]},
            ]
        }
    )
    candidate = next(
        c
        for c in V.detect_candidates(p)
        if c["source"].get("children_path") == "packages"
    )
    assert candidate["source"]["path"] == "editions"
    assert candidate["count"] == 3
    rows = V.from_embedded(p, candidate["source"])
    assert [(r["parent"]["raw"]["year"], r["raw"].get("sku")) for r in rows] == [
        ("2021", None),
        ("2022", None),
        ("2023", "003"),
    ]


def mapping(src):
    return {
        "schema_version": 1,
        "template_id": "product",
        "match": {"selector": "h1"},
        "fields": [
            {
                "name": "title",
                "type": "text",
                "sources": [{"kind": "css", "selector": "h1", "extract": "text"}],
            }
        ],
        "variants": {"sources": [src]},
    }


def test_contract_accepts_relative_children_path_for_embedded_json():
    assert not list(validator("mapping.schema.json").iter_errors(mapping(source())))


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "shop_json", "platform": "shopify"},
        {"kind": "jsonld", "schema_type": "Product", "path": "offers"},
        {"children_path": ""},
        {"children_path": "unsafe[expression]"},
        {"children_path": "x" * 301},
    ],
)
def test_contract_rejects_unsupported_or_invalid_children_paths(changes):
    assert list(
        validator("mapping.schema.json").iter_errors(mapping(source(**changes)))
    )
