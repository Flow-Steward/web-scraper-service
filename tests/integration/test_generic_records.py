"""Final records respect per-edition DOM and declarative inclusion constraints."""

import copy
import json

from tests.conftest import check, ensure_mappings, fast, merged_config, profile, start

SITE = "variants.test:8602"
URL = "http://variants.test:8602/v/editions/fixture"
MAPPING = {
    "schema_version": 1,
    "template_id": "editions",
    "match": {"url_patterns": ["/v/editions/"]},
    "fields": [
        {
            "name": "title",
            "type": "text",
            "required": True,
            "sources": [{"kind": "css", "selector": "h1", "extract": "text"}],
        },
        {
            "name": "description",
            "type": "html",
            "sources": [{"kind": "css", "selector": ".parent", "extract": "html"}],
        },
    ],
    "variants": {
        "sources": [
            {
                "kind": "embedded_json",
                "selector": "script#choices",
                "path": "state.choices",
                "format": "generic",
            }
        ],
        "fields": [
            {"name": "sku", "path": "sku"},
            {
                "name": "allowed",
                "path": "raw.enabled",
                "type": "boolean",
                "fallback_to_page": False,
            },
            {
                "name": "description",
                "selector_template": 'section[data-edition="{id}"] .description',
                "extract": "html",
                "type": "html",
                "fallback_to_page": False,
            },
        ],
    },
}


def run(service, mapping, key):
    ensure_mappings(service, SITE, mapping)
    cfg = merged_config(
        service,
        {
            "discovery_mode": "url_list",
            "urls": [{"url": URL}],
            "fetch_mode": "http",
            "fields": ["title", "description", "sku"],
            "required_fields": ["title"],
            "max_repairs_per_template": 0,
            "max_repairs_per_job": 0,
            **fast(max_concurrency=1),
        },
    )
    result = service.wait_run(start(service, cfg, key).json()["run_id"])
    records = [
        json.loads(line)
        for line in service.get(result["result"]["download_path"]).text.splitlines()
    ]
    return result, sorted(records, key=lambda row: row["item_index"])


def test_runtime_filters_after_indexing_and_reads_variant_dom(service, fixture_world):
    _, original = run(service, MAPPING, "generic-editions-all")
    selected = copy.deepcopy(MAPPING)
    selected["record_filter"] = [{"field": "allowed", "op": "eq", "value": True}]
    result, records = run(service, selected, "generic-editions-selected")
    assert result["state"] == "completed"
    assert [r["item_index"] for r in records] == [0, 2]
    assert [r["data"]["sku"] for r in records] == ["0001-A", "0001-C"]
    assert "First edition notes" in records[0]["data"]["description"]
    assert records[1]["data"]["description"] is None
    assert [r["record_id"] for r in records] == [
        original[0]["record_id"],
        original[2]["record_id"],
    ]


def test_all_filtered_variants_do_not_emit_parent_record(service, fixture_world):
    mapping = copy.deepcopy(MAPPING)
    mapping["record_filter"] = [{"field": "sku", "op": "eq", "value": "absent"}]
    result, records = run(service, mapping, "generic-editions-none")
    assert result["state"] == "completed" and result["outcome"] == "complete"
    assert records == []
    assert result["counters"]["records_extracted"] == 0


def test_nested_candidates_are_optional_and_mapping_preview_filters_rows(
    service, fixture_world
):
    cfg = merged_config(service, {"site_url": URL, "fetch_mode": "http"})
    body = check(
        service.post(
            "/v1/samples", {"config": cfg, "profile": profile(), "urls": [URL]}
        ),
        "samples.schema.json",
        200,
    )
    sample = body["samples"][0]
    assert sample["variant_candidates"]
    assert sample.get("variant_sources", []) == []
    mapping = copy.deepcopy(MAPPING)
    mapping["record_filter"] = [{"field": "allowed", "op": "eq", "value": True}]
    test = check(
        service.post(
            "/v1/mappings/test",
            {
                "site": SITE,
                "mappings": [mapping],
                "sample_ids": [sample["sample_id"]],
                "required_fill_threshold": 0.9,
            },
        ),
        "mapping_test.schema.json#/$defs/response",
        200,
    )
    variants = test["samples"][0]["variants"]
    assert (
        variants["count"] == 3
        and variants["included_count"] == 2
        and variants["excluded_count"] == 1
    )
    assert [i["variant"]["index"] for i in variants["items"]] == [0, 2]


def test_required_fields_are_checked_on_every_included_variant(service, fixture_world):
    mapping = copy.deepcopy(MAPPING)
    mapping["fields"][1]["required"] = True
    result, records = run(service, mapping, "generic-editions-required")
    assert result["outcome"] == "partial"
    missing = service.get(f"/v1/runs/{result['run_id']}/unextracted").json()["items"]
    assert any("description" in row.get("missing_fields", []) for row in missing)
    assert records == []


def test_missing_variant_evidence_cannot_pass_parent_availability_filter(
    service, fixture_world
):
    mapping = copy.deepcopy(MAPPING)
    mapping["fields"].append(
        {
            "name": "allowed",
            "type": "boolean",
            "sources": [
                {
                    "kind": "css",
                    "selector": ".parent",
                    "extract": "attribute",
                    "attribute": "data-allowed",
                }
            ],
        }
    )
    mapping["variants"]["sources"][0]["selector"] = "script#missing-state"
    mapping["record_filter"] = [{"field": "allowed", "op": "eq", "value": True}]
    result, records = run(service, mapping, "missing-editions-availability")
    assert result["outcome"] == "partial"
    assert records == []
    missing = service.get(f"/v1/runs/{result['run_id']}/unextracted").json()["items"]
    assert any("allowed" in row.get("missing_fields", []) for row in missing)
