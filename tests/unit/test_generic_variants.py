"""Generic state arrays and declarative record constraints, without site adapters."""

import importlib
import importlib.util
import json

import pytest
from app.core.schemas import validator
from app.extraction import variants as V
from app.extraction.page import Page


def _page(state, *, ident="state"):
    return Page(
        '<html><body><script>console.log(1)</script><script type="application/json" id="'
        + ident
        + '">'
        + json.dumps(state)
        + "</script></body></html>",
        "https://example.test/item",
    )


def test_nested_candidate_exposes_raw_values_without_declaring_variants():
    page = _page(
        {
            "props": {
                "entry": {
                    "choices": [
                        {
                            "code": "x",
                            "dimension": "small",
                            "purchase": {"enabled": True},
                            "amount": "12.50",
                        },
                        {
                            "code": "y",
                            "dimension": "large",
                            "purchase": {"enabled": False},
                            "amount": "18.00",
                        },
                    ]
                }
            }
        }
    )
    detect = getattr(V, "detect_candidates", None)
    assert callable(detect), "nested arrays must be visible as optional mapping candidates"
    candidates = detect(page)
    candidate = next(c for c in candidates if c["source"].get("path") == "props.entry.choices")
    assert candidate["count"] == 2
    assert candidate["raw_examples"][0]["purchase"]["enabled"] is True
    assert "purchase.enabled" in candidate["raw_paths"]
    assert candidate["evidence"]
    assert V.detect_sources(page) == []
    rows = V.from_embedded(page, candidate["source"])
    assert [row["raw"]["code"] for row in rows] == ["x", "y"]


def test_candidate_inside_an_array_uses_a_resolvable_numeric_path():
    page = _page({"groups": [{"selections": [{"code": "one"}, {"code": "two"}]}]})
    detect = getattr(V, "detect_candidates", None)
    assert callable(detect), "nested object arrays must be discoverable"
    candidate = next(c for c in detect(page) if c["source"].get("path") == "groups.0.selections")
    assert [v["raw"]["code"] for v in V.from_embedded(page, candidate["source"])] == [
        "one",
        "two",
    ]


def test_candidate_discovery_bounds_depth_output_and_script_size():
    detect = getattr(V, "detect_candidates", None)
    assert callable(detect), "candidate discovery needs resource bounds"
    state = {f"choices{i}": [{"code": "a", "text": "x" * 2000}, {"code": "b"}] for i in range(100)}
    candidates = detect(_page(state))
    assert 0 < len(candidates) <= 12
    assert len(json.dumps(candidates)) < 40000
    deep = {"choices": [{"code": "a"}, {"code": "b"}]}
    for _ in range(30):
        deep = {"nested": deep}
    assert detect(_page(deep)) == []
    assert detect(_page({"padding": "x" * 600000, "choices": [{"code": "a"}]})) == []


def test_idless_json_script_selector_resolves_the_actual_element():
    page = Page(
        '<script>const ordinary = 1;</script><script type="application/json">'
        '{"variants":[{"id":10,"price":1},{"id":20,"price":2}]}</script>',
        "https://example.test/item",
    )
    sources = V.detect_sources(page)
    assert len(sources) == 1
    assert [v["id"] for v in V.from_embedded(page, sources[0]["source"])] == [10, 20]


def test_embedded_terminal_arrays_are_flattened_once():
    page = _page({"groups": [{"rows": [{"id": 10}]}, {"rows": [{"id": 20}]}]})
    values = V.from_embedded(
        page, {"selector": "script#state", "path": "groups.*.rows", "format": "generic"}
    )
    assert [v["id"] for v in values] == [10, 20]


def test_authoritative_variant_field_does_not_inherit_parent_value():
    mapping = {
        "fields": [],
        "variants": {
            "fields": [
                {
                    "name": "allowed",
                    "path": "raw.purchase.enabled",
                    "type": "boolean",
                    "fallback_to_page": False,
                },
                {"name": "currency", "path": "currency"},
            ]
        },
    }
    data, filled = V.apply_variant(
        mapping,
        {"allowed": True, "currency": "EUR"},
        V.from_generic({"id": "one"}),
        "https://example.test/",
    )
    assert data == {"allowed": None, "currency": "EUR"}
    assert filled == {"allowed": False, "currency": False}


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"allowed": True, "quantity": 2}, True),
        ({"allowed": False, "quantity": 2}, False),
        ({"allowed": 1, "quantity": 2}, False),
        ({"allowed": "true", "quantity": 2}, False),
        ({"allowed": True, "quantity": True}, False),
        ({"allowed": True, "quantity": "2"}, False),
        ({"allowed": True, "quantity": 0}, False),
        ({"allowed": True}, False),
        ({"allowed": None, "quantity": 2}, False),
    ],
)
def test_record_filter_is_typed_and_requires_every_condition(data, expected):
    assert importlib.util.find_spec("app.extraction.record_filter"), (
        "record filtering must have an executable generic implementation"
    )
    matches = importlib.import_module("app.extraction.record_filter").record_matches
    mapping = {
        "record_filter": [
            {"field": "allowed", "op": "eq", "value": True},
            {"field": "quantity", "op": "gt", "value": 0},
        ]
    }
    assert matches(mapping, data) is expected
    assert matches({}, data) is True


def _mapping():
    return {
        "schema_version": 1,
        "template_id": "entry",
        "match": {"selector": "h1"},
        "fields": [
            {
                "name": "allowed",
                "type": "boolean",
                "sources": [{"kind": "meta", "name": "allowed"}],
            },
        ],
    }


def test_mapping_contract_accepts_constraints_and_authoritative_variant_fields():
    mapping = _mapping()
    mapping["record_filter"] = [{"field": "allowed", "op": "eq", "value": True}]
    mapping["variants"] = {
        "sources": [{"kind": "embedded_json", "selector": "script#state"}],
        "fields": [
            {
                "name": "allowed",
                "path": "raw.purchase.enabled",
                "type": "boolean",
                "fallback_to_page": False,
            },
        ],
    }
    assert not list(validator("mapping.schema.json").iter_errors(mapping))


@pytest.mark.parametrize(
    "conditions",
    [
        [{"field": "allowed", "op": "eval", "value": True}],
        [{"field": "allowed", "op": "gt", "value": True}],
        [{"field": "allowed", "op": "gt", "value": "1"}],
        [{"field": "allowed", "op": "eq", "value": {}}],
        [{"field": "allowed", "op": "eq", "value": True}] * 21,
    ],
)
def test_mapping_contract_rejects_executable_or_unbounded_constraints(conditions):
    mapping = _mapping()
    mapping["record_filter"] = conditions
    assert list(validator("mapping.schema.json").iter_errors(mapping))


def test_variant_dom_fields_join_by_identity_without_parent_fallback():
    page = Page(
        """<h1>Shared title</h1>
        <section class="edition" data-choice="first"><p>First edition <b>notes</b></p><img src="/first.jpg"></section>
        <section class="edition" data-choice="second"><p>Second edition notes</p><img src="/second.jpg"></section>""",
        "https://example.test/item",
    )
    mapping = {
        "fields": [],
        "variants": {
            "fields": [
                {
                    "name": "description",
                    "selector_template": 'section.edition[data-choice="{id}"] p',
                    "extract": "html",
                    "type": "html",
                    "fallback_to_page": False,
                },
                {
                    "name": "image",
                    "selector_template": 'section.edition[data-choice="{id}"] img',
                    "extract": "attribute",
                    "attribute": "src",
                    "type": "image_url",
                    "fallback_to_page": False,
                },
            ]
        },
    }
    import inspect

    assert "page" in inspect.signature(V.variant_rows).parameters, (
        "variant fields must join the matching DOM fragment"
    )
    variants = [V.from_generic({"id": value}) for value in ("first", "second", "missing")]
    rows = V.variant_rows(
        mapping,
        {"description": "Parent description", "image": "/parent.jpg"},
        variants,
        "embedded_json",
        page.base_url,
        page=page,
    )
    assert rows[0][0]["description"] == "First edition <b>notes</b>"
    assert rows[1][0]["description"] == "Second edition notes"
    assert [row[0]["image"] for row in rows] == [
        "https://example.test/first.jpg",
        "https://example.test/second.jpg",
        None,
    ]
    assert rows[2][0]["description"] is None


def test_variant_dom_selector_escapes_identity_values():
    page = Page(
        '<section data-choice="plain"><p>Wrong edition</p></section>',
        "https://example.test/item",
    )
    import inspect

    assert "page" in inspect.signature(V.variant_rows).parameters, (
        "variant DOM lookups need safe interpolation"
    )
    mapping = {
        "fields": [],
        "variants": {
            "fields": [
                {
                    "name": "description",
                    "selector_template": '[data-choice="{id}"] p',
                    "extract": "text",
                    "fallback_to_page": False,
                }
            ]
        },
    }
    rows = V.variant_rows(
        mapping,
        {"description": "Parent"},
        [V.from_generic({"id": 'absent"], section[data-choice="plain'})],
        "embedded_json",
        page.base_url,
        page=page,
    )
    assert rows[0][0]["description"] is None


@pytest.mark.parametrize(
    "field",
    [
        {
            "name": "detail",
            "selector_template": '[data-choice="{id}"]',
            "extract": "text",
        },
        {
            "name": "detail",
            "selector_template": '[data-sku="{sku}"]',
            "extract": "html",
        },
    ],
)
def test_mapping_contract_accepts_safe_variant_dom_templates(field):
    mapping = _mapping()
    mapping["variants"] = {
        "sources": [{"kind": "embedded_json", "selector": "script#state"}],
        "fields": [field],
    }
    assert not list(validator("mapping.schema.json").iter_errors(mapping))


@pytest.mark.parametrize(
    "field",
    [
        {
            "name": "detail",
            "selector_template": '[data-choice="{raw.code}"]',
            "extract": "text",
        },
        {"name": "detail", "selector_template": '[data-choice="{id}"]'},
        {
            "name": "detail",
            "selector_template": '[data-choice="{id}"]',
            "extract": "attribute",
        },
        {
            "name": "detail",
            "selector_template": '[data-choice="{id}"]',
            "extract": "text",
            "path": "description",
        },
    ],
)
def test_mapping_contract_rejects_ambiguous_or_incomplete_variant_dom_templates(field):
    mapping = _mapping()
    mapping["variants"] = {
        "sources": [{"kind": "embedded_json", "selector": "script#state"}],
        "fields": [field],
    }
    assert list(validator("mapping.schema.json").iter_errors(mapping))


def test_duplicate_script_ids_keep_each_candidate_bound_to_its_own_block():
    page = Page(
        '<script type="application/json" id="state">'
        '{"choices":[{"code":"first"}]}</script>'
        '<script type="application/json" id="state">'
        '{"choices":[{"code":"second"}]}</script>',
        "https://example.test/item",
    )
    candidates = V.detect_candidates(page)
    assert len(candidates) == 2
    assert [
        V.from_embedded(page, candidate["source"])[0]["raw"]["code"] for candidate in candidates
    ] == ["first", "second"]


def test_missing_variant_source_does_not_inherit_authoritative_parent_fields():
    mapping = {
        "fields": [],
        "variants": {
            "fields": [
                {
                    "name": "allowed",
                    "path": "raw.enabled",
                    "type": "boolean",
                    "fallback_to_page": False,
                },
                {
                    "name": "description",
                    "path": "description",
                    "fallback_to_page": False,
                },
                {"name": "currency", "path": "currency"},
            ]
        },
    }
    rows = V.variant_rows(
        mapping,
        {"allowed": True, "description": "Parent notes", "currency": "EUR"},
        [],
        None,
        "https://example.test/",
    )
    assert rows[0][0] == {"allowed": None, "description": None, "currency": "EUR"}
    assert rows[0][1] is None
    legacy = {
        "fields": [],
        "variants": {
            "fields": [
                {"name": "allowed", "path": "raw.enabled", "type": "boolean"},
                {"name": "description", "path": "description"},
            ]
        },
    }
    assert V.variant_rows(
        legacy,
        {"allowed": True, "description": "Parent notes"},
        [],
        None,
        "https://example.test/",
    )[0][0] == {"allowed": True, "description": "Parent notes"}


def test_candidate_examples_include_nested_choice_fields_and_string_arrays():
    technical = {
        "voltage": 12,
        "capacity": 500,
        "mass": 0.4,
        "region": "north",
        "material": "glass",
        "format": "small",
        "category": "standard",
        "enabled": True,
        "finish": "clear",
        "code": "detail-a",
        "series": "S",
        "temperature": 18,
    }
    page = _page(
        {
            "entries": [
                {
                    "code": "entry-a",
                    "choices": [technical, dict(technical, voltage=24)],
                    "features": ["portable", "repairable", "additional"],
                },
                {
                    "code": "entry-b",
                    "choices": [dict(technical, voltage=36)],
                    "features": ["compact"],
                },
            ]
        }
    )

    candidates = V.detect_candidates(page)
    candidate = next(c for c in candidates if c["source"].get("path") == "entries")
    example = candidate["raw_examples"][0]

    assert example["choices"] == [technical, dict(technical, voltage=24)]
    assert example["features"] == ["portable", "repairable"]
    assert "choices.0.voltage" in candidate["raw_paths"]
    assert "choices.1.voltage" in candidate["raw_paths"]
    assert "features.0" in candidate["raw_paths"]
    assert V.json_path(example, "choices.0.voltage") == [12]
    assert V.json_path(example, "features.1") == ["repairable"]
    assert any(c["source"].get("path") == "entries.0.choices" for c in candidates)
    assert V.detect_sources(page) == []


def test_nested_array_examples_keep_source_indices_after_empty_items():
    example, paths = V._raw_example({"choices": [{}, {"capacity": 750}, {"capacity": 999}]})

    assert example == {"choices": [{}, {"capacity": 750}]}
    assert paths == ["choices.1.capacity"]
    assert V.json_path(example, paths[0]) == [750]


def test_nested_array_examples_share_the_scalar_and_string_budgets():
    fields = {f"attribute{i}": "x" * 250 for i in range(40)}
    example, paths = V._raw_example({"matrix": [[fields] * 50] * 50, "later": "unvisited"})

    assert len(paths) == 30
    assert paths[0] == "later"
    assert all(path.startswith("matrix.0.0.attribute") for path in paths[1:])
    assert all(len(V.json_path(example, path)[0]) == 180 for path in paths[1:])
    assert example["later"] == "unvisited"
    assert len(json.dumps(example)) < 7000


def test_nested_array_examples_keep_the_existing_container_depth_limit():
    example, paths = V._raw_example(
        {
            "visible": [[{"name": "example"}]],
            "too_deep": [[[[{"hidden": "not exposed"}]]]],
        }
    )

    assert V.json_path(example, "visible.0.0.name") == ["example"]
    assert "visible.0.0.name" in paths
    assert all("hidden" not in path for path in paths)
    assert "not exposed" not in json.dumps(example)


def test_candidate_preview_keeps_late_parent_flags_ahead_of_nested_details():
    identity = {f"identity{i}": f"value-{i}" for i in range(20)}
    details = {f"technical{i}": i for i in range(12)}
    page = _page(
        {
            "entries": [
                {
                    **identity,
                    "choices": [details, dict(details, technical0=99)],
                    "sellable": True,
                }
            ]
        }
    )

    candidate = next(c for c in V.detect_candidates(page) if c["source"].get("path") == "entries")
    example = candidate["raw_examples"][0]

    assert example["sellable"] is True
    assert "sellable" in candidate["raw_paths"]
    assert all(example[key] == value for key, value in identity.items())
    assert len(candidate["raw_paths"]) == 30
    assert V.json_path(example, "sellable") == [True]
