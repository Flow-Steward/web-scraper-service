"""Findings on target pages:

* a mapping test lists fields no sample filled (`empty_fields`, `fields_never_filled`),
  and the target-page guess prefers leaf pages with product signals over content pages.
* with `target_page_selector` set, a page without it is `not_target` even when a
  template matches it by URL.
"""

from __future__ import annotations

from tests.conftest import PRODUCT_MAPPING, SHOP, SHOP_SITE, check, ensure_mappings, fast, merged_config, profile, start

MIXED = "http://mixed.test:8602/"


def test_guess_prefers_product_pages_over_a_larger_group_of_content_pages(service, fixture_world):
    cfg = merged_config(service, {"site_url": MIXED, "fetch_mode": "http", "sample_count": 3})
    body = check(service.post("/v1/samples", {"config": cfg, "profile": profile()}), "samples.schema.json", 200)
    assert len(body["samples"]) == 3 and all("/produkt/" in s["url"] for s in body["samples"]), [s["url"] for s in body["samples"]]
    guessed = [w for w in body["warnings"] if w["code"] == "target_pages_guessed"]
    assert guessed and "/produkt/*" in guessed[0]["message"] and "price-like text" in guessed[0]["message"], guessed


def test_mapping_test_names_fields_no_sample_filled(service, fixture_world):
    cfg = merged_config(service, {"site_url": "http://nold.test:8602/", "fetch_mode": "http", "sample_count": 3})
    samples = check(service.post("/v1/samples", {"config": cfg, "profile": profile()}), "samples.schema.json", 200)
    ids = [s["sample_id"] for s in samples["samples"]]
    assert len(ids) == 3
    mapping = {
        "schema_version": 1, "template_id": "page", "label": "Page", "match": {"url_patterns": ["/produkt/"]},
        "fields": [
            {"name": "title", "type": "text", "required": True, "sources": [{"kind": "css", "selector": "h1", "extract": "text"}]},
            {"name": "price", "type": "price", "sources": [{"kind": "css", "selector": ".no-such-price", "extract": "text"}]},
            {"name": "part_number", "type": "text", "sources": [{"kind": "css", "selector": ".no-such-sku", "extract": "text"}]},
        ],
    }
    body = check(service.post("/v1/mappings/test", {"site": "nold.test:8602", "mappings": [mapping], "sample_ids": ids, "required_fill_threshold": 0.9}),
                 "mapping_test.schema.json#/$defs/response", 200)
    assert body["passed"] is True  # the pass rule is unchanged: only title is required
    tmpl = body["templates"][0]
    assert tmpl["empty_fields"] == ["price", "part_number"], tmpl
    warn = [w for w in body["warnings"] if w["code"] == "fields_never_filled"]
    assert len(warn) == 1 and warn[0]["field"] == "mappings[0].fields", body["warnings"]
    assert warn[0]["message"].startswith("template page: price, part_number filled on 0 of 3 matched samples (the test passed on the required fields: title)")


def test_target_page_selector_gates_a_matching_template(service, fixture_world):
    ensure_mappings(service, SHOP_SITE, PRODUCT_MAPPING)
    cfg = merged_config(service, {"site_url": SHOP + "/", "discovery_mode": "sitemap", "fetch_mode": "http", "max_pages": 6,
                                  "url_include": ["/produkt/"], "target_page_selector": "[data-no-such-marker]", **fast()})
    run = service.wait_run(start(service, cfg, "a30-selector-gate").json()["run_id"])
    counters = run["counters"]
    assert counters["records_extracted"] == 0 and counters["pages_by_class"].get("not_target", 0) >= 3 and "extracted" not in counters["pages_by_class"], counters
    # the same crawl with a selector the product pages carry extracts them
    cfg["target_page_selector"] = "div.product-detail"
    run = service.wait_run(start(service, cfg, "a30-selector-pass").json()["run_id"])
    assert run["counters"]["records_extracted"] >= 3, run["counters"]
