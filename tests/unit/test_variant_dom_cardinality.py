"""Variant DOM selectors respect the parent field's declared cardinality."""

import pytest

from app.extraction import variants as V
from app.extraction.page import Page

BASE = "https://example.test/item"


def _mapping(*, multiple=True, field_type="image_url", fallback_to_page=False):
    return {
        "fields": [{"name": "value", "type": field_type, "multiple": multiple}],
        "variants": {
            "fields": [
                {
                    "name": "value",
                    "selector_template": "#variant-{id} img",
                    "extract": "attribute",
                    "attribute": "src",
                    "fallback_to_page": fallback_to_page,
                }
            ]
        },
    }


@pytest.mark.parametrize(
    ("multiple", "expected"),
    [
        (True, ["https://example.test/one.jpg", "https://example.test/two.jpg"]),
        (False, "https://example.test/one.jpg"),
    ],
)
def test_variant_dom_images_preserve_parent_cardinality(multiple, expected):
    page = Page(
        '<section id="variant-first"><img src="/one.jpg"><img src="/two.jpg"></section>'
        '<section id="variant-second"><img src="/unrelated.jpg"></section>',
        BASE,
    )

    rows = V.variant_rows(
        _mapping(multiple=multiple),
        {"value": None},
        [V.from_generic({"id": "first"})],
        "embedded_json",
        BASE,
        page=page,
    )

    assert rows[0][0]["value"] == expected
    assert rows[0][2]["value"] is True


def test_variant_dom_values_apply_transforms_in_order_before_item_coercion():
    page = Page(
        '<section id="variant-first">'
        '<span> value:0 </span><span>invalid</span><span> value:2 </span>'
        '</section>',
        BASE,
    )
    mapping = _mapping(field_type="number")
    mapping["variants"]["fields"][0].update(
        selector_template="#variant-{id} span",
        extract="text",
        transforms=[
            {"op": "trim"},
            {"op": "regex_replace", "pattern": "^value:", "replacement": ""},
        ],
    )

    data, filled = V.apply_variant(
        mapping, {"value": [9.0]}, V.from_generic({"id": "first"}), BASE, page=page
    )

    assert data["value"] == [0.0, 2.0]
    assert filled["value"] is True


@pytest.mark.parametrize("fallback_to_page", [False, True])
@pytest.mark.parametrize(
    "html",
    [
        '<section id="variant-other"><img src="/other.jpg"></section>',
        '<section id="variant-first"><img><img src="javascript:invalid"></section>',
    ],
)
def test_optional_missing_dom_values_keep_existing_fallback_semantics(
    fallback_to_page, html
):
    page = Page(html, BASE)
    mapping = _mapping(fallback_to_page=fallback_to_page)
    mapping["fields"][0]["required"] = False
    parent = {"value": ["https://example.test/parent.jpg"]}

    data, filled = V.apply_variant(
        mapping, parent, V.from_generic({"id": "first"}), BASE, page=page
    )

    assert data["value"] == (parent["value"] if fallback_to_page else None)
    assert filled["value"] is False


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("raw.images", ["https://example.test/one.jpg", "https://example.test/two.jpg"]),
        ("image", ["https://example.test/one.jpg"]),
    ],
)
def test_parent_multiple_does_not_nest_json_or_normalized_variant_values(path, expected):
    mapping = _mapping()
    mapping["variants"]["fields"][0] = {
        "name": "value", "path": path, "fallback_to_page": False
    }
    variant = V.from_generic({"images": ["/one.jpg", "/two.jpg"], "image": "/one.jpg"})

    data, filled = V.apply_variant(mapping, {"value": []}, variant, BASE)

    assert data["value"] == expected
    assert filled["value"] is True
