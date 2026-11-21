"""Useful SSR links must not hide a listing's hydration-only pagination."""

import json

import pytest

from tests.conftest import check, ensure_mappings, fast, merged_config, profile, start

BASE = "http://variants.test:8602"
MAPPING = {
    "schema_version": 1,
    "template_id": "productive_sheet",
    "match": {"url_patterns": ["/v/plain-item/"]},
    "fields": [
        {
            "name": "title",
            "type": "text",
            "required": True,
            "sources": [{"kind": "css", "selector": "h1", "extract": "text"}],
        }
    ],
}


def config(service, mode):
    return merged_config(
        service,
        {
            "site_url": BASE + "/v/productive-shell/category",
            "discovery_mode": "crawl",
            "fetch_mode": mode,
            "browser": "chromium",
            "url_include": ["/v/plain-item/"],
            "crawl_include": ["/v/productive-shell/"],
            "max_pages": 16,
            "sample_count": 3,
            **fast(max_concurrency=1),
        },
    )


def expected_urls(mode):
    names = ["initial", "first", "second"] if mode == "auto" else ["initial"]
    return {BASE + "/v/plain-item/" + name for name in names}


@pytest.mark.parametrize("mode", ["auto", "http"])
def test_samples_render_scripted_listings_despite_fresh_static_navigation(
    service, fixture_world, mode
):
    ensure_mappings(service, "variants.test:8602", MAPPING)
    body = check(
        service.post(
            "/v1/samples",
            {
                "config": config(service, mode),
                "profile": profile(),
            },
        ),
        "samples.schema.json",
        200,
    )

    assert {sample["url"] for sample in body["samples"]} == expected_urls(mode)


@pytest.mark.parametrize("mode", ["auto", "http"])
def test_runtime_renders_scripted_listings_despite_fresh_static_navigation(
    service, fixture_world, mode
):
    ensure_mappings(service, "variants.test:8602", MAPPING)
    run = service.wait_run(
        start(
            service, config(service, mode), "productive-scripted-listing-" + mode
        ).json()["run_id"]
    )
    assert run["state"] == "completed" and run["outcome"] == "complete"
    records = [
        json.loads(line)
        for line in service.get(run["result"]["download_path"]).text.splitlines()
    ]

    assert {record["source_url"] for record in records} == expected_urls(mode)
    if mode == "http":
        assert run["counters"]["browser_fetches"] == 0
