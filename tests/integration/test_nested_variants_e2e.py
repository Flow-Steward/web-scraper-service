"""The public samples/test/run/CSV flow preserves parent and child identities."""

import csv
import io
import json

from tests.conftest import check, ensure_mappings, fast, merged_config, profile, start

SITE = "variants.test:8602"
URL = "http://" + SITE + "/v/nested-editions/example"
MAPPING = {
    "schema_version": 1,
    "template_id": "nested",
    "match": {"url_patterns": ["/v/nested-editions/"]},
    "fields": [
        {
            "name": name,
            "type": "text",
            "required": True,
            "sources": [{"kind": "css", "selector": selector, "extract": "text"}],
        }
        for name, selector in [
            ("title", "h1"),
            ("sku", ".absent-sku"),
            ("year", ".absent-year"),
        ]
    ],
    "variants": {
        "sources": [
            {
                "kind": "embedded_json",
                "selector": "script#state",
                "path": "editions",
                "children_path": "packages",
                "format": "generic",
            }
        ],
        "fields": [
            {"name": "sku", "path": "raw.sku", "fallback_to_page": False},
            {"name": "year", "path": "parent.raw.year", "fallback_to_page": False},
            {"name": "notes", "path": "parent.raw.notes", "fallback_to_page": False},
            {"name": "format", "path": "raw.format", "fallback_to_page": False},
            {"name": "packaging", "path": "raw.packaging", "fallback_to_page": False},
            {
                "name": "available",
                "path": "raw.available",
                "type": "boolean",
                "fallback_to_page": False,
            },
        ],
    },
}


def test_nested_context_survives_samples_mapping_test_run_and_csv(
    service, fixture_world
):
    cfg = merged_config(
        service,
        {
            "site_url": URL,
            "fetch_mode": "http",
            "discovery_mode": "url_list",
            "urls": [{"url": URL}],
            **fast(max_concurrency=1),
        },
    )
    samples = check(
        service.post(
            "/v1/samples", {"config": cfg, "profile": profile(), "urls": [URL]}
        ),
        "samples.schema.json",
        200,
    )
    candidate = next(
        c
        for c in samples["samples"][0]["variant_candidates"]
        if c["source"].get("children_path") == "packages"
    )
    assert candidate["count"] == 3 and candidate["parent_count"] == 2
    tested = check(
        service.post(
            "/v1/mappings/test",
            {
                "site": SITE,
                "mappings": [MAPPING],
                "sample_ids": [samples["samples"][0]["sample_id"]],
                "required_fill_threshold": 1.0,
            },
        ),
        "mapping_test.schema.json#/$defs/response",
        200,
    )
    assert tested["passed"] is True
    ensure_mappings(service, SITE, MAPPING)
    run = service.wait_run(start(service, cfg, "nested-context").json()["run_id"])
    assert (run["outcome"], run["stop_reason"], run["result"]["record_count"]) == (
        "complete",
        "finished",
        3,
    )
    records = [
        json.loads(line)
        for line in service.get(run["result"]["download_path"]).text.splitlines()
    ]
    by_sku = {r["data"]["sku"]: r["data"] for r in records}
    assert [
        (
            by_sku[sku]["year"],
            by_sku[sku]["format"],
            by_sku[sku]["packaging"],
            by_sku[sku]["available"],
        )
        for sku in ["001", "002", "003"]
    ] == [
        ("2023", "1 x 150 cl", "Wood presentation box", False),
        ("2023", "6 x 75 cl", None, True),
        ("2024", "1 x 75 cl", "Individual carton", None),
    ]
    assert len({r["record_id"] for r in records}) == 3
    csv_text = service.get(run["result"]["csv"]["download_path"]).content.decode(
        "utf-8-sig"
    )
    rows = {
        r["data.sku"]: r for r in csv.DictReader(io.StringIO(csv_text), delimiter=";")
    }
    assert len(rows) == 3
    assert rows["001"]["data.available"] == "false"
    assert rows["003"]["data.available"] == ""
    assert rows["002"]["data.packaging"] == ""
    assert rows["001"]["data.year"] == "2023" and rows["003"]["data.year"] == "2024"
