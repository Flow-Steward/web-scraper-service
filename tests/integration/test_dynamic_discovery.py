import json

import pytest

from tests.conftest import check, ensure_mappings, fast, merged_config, profile, start

BASE = "http://variants.test:8602"
SHEET = {
    "schema_version": 1,
    "template_id": "plain_sheet",
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


def config(service, fetch_mode="browser", listing_kind="listing"):
    return merged_config(
        service,
        {
            "site_url": BASE + f"/v/{listing_kind}/category",
            "discovery_mode": "crawl",
            "fetch_mode": fetch_mode,
            "browser": "chromium",
            "url_include": ["/v/plain-item/"],
            "crawl_include": [f"/v/{listing_kind}/"],
            "max_pages": 8,
            "sample_count": 2,
            **fast(max_concurrency=1),
        },
    )


@pytest.mark.parametrize(
    ("fetch_mode", "listing_kind"),
    [
        pytest.param("browser", "listing", id="browser"),
        pytest.param("auto", "listing", id="auto"),
        pytest.param("auto", "shell-listing", id="auto-shell"),
    ],
)
def test_samples_include_products_revealed_by_javascript_pagination(
    service, fixture_world, fetch_mode, listing_kind
):
    ensure_mappings(service, "variants.test:8602", SHEET)
    body = check(
        service.post(
            "/v1/samples",
            {"config": config(service, fetch_mode, listing_kind), "profile": profile()},
        ),
        "samples.schema.json",
        200,
    )
    assert {s["url"] for s in body["samples"]} == {
        BASE + "/v/plain-item/first",
        BASE + "/v/plain-item/second",
    }


@pytest.mark.parametrize(
    ("fetch_mode", "listing_kind"),
    [
        pytest.param("browser", "listing", id="browser"),
        pytest.param("auto", "listing", id="auto"),
        pytest.param("auto", "shell-listing", id="auto-shell"),
    ],
)
def test_full_crawl_collects_products_from_all_browser_states(
    service, fixture_world, fetch_mode, listing_kind
):
    ensure_mappings(service, "variants.test:8602", SHEET)
    run = service.wait_run(
        start(
            service,
            config(service, fetch_mode, listing_kind),
            "dynamic-" + fetch_mode + "-" + listing_kind + "-crawl",
        ).json()["run_id"]
    )
    assert run["state"] == "completed" and run["outcome"] == "complete"
    records = [
        json.loads(line) for line in service.get(run["result"]["download_path"]).text.splitlines()
    ]
    assert {r["source_url"] for r in records} == {
        BASE + "/v/plain-item/first",
        BASE + "/v/plain-item/second",
    }


def test_javascript_navigation_cannot_bypass_the_job_page_budget(service, fixture_world):
    ensure_mappings(service, "variants.test:8602", SHEET)
    bounded = config(service)
    bounded["max_pages"] = 1
    run = service.wait_run(start(service, bounded, "dynamic-browser-bounded").json()["run_id"])
    assert run["state"] == "completed"
    assert run["outcome"] == "partial"
    assert any(e.get("code") == "navigation_partial" for e in run.get("errors", [])), run.get(
        "errors"
    )
