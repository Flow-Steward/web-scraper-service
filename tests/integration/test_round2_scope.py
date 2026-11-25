"""A crawl started on a category stays in it (scope.test fixture)."""

from __future__ import annotations

from tests.conftest import check, ensure_mappings, fast, merged_config, profile, start

S = "http://scope.test:8602"
SITE = "scope.test:8602"
ITEM = {"schema_version": 1, "template_id": "item", "match": {"url_patterns": ["-p\\d+$"]}, "fields": [
    {"name": "title", "type": "text", "required": True, "sources": [{"kind": "jsonld", "schema_type": "Product", "path": "name"}]},
    {"name": "price", "type": "price", "sources": [{"kind": "jsonld", "schema_type": "Product", "path": "offers.price"}]}]}


def test_stay_in_section_takes_the_category_and_its_products_only(service, fixture_world):
    ensure_mappings(service, SITE, ITEM)
    fixture_world.set(8602, reset_hits=True)
    cfg = merged_config(service, {"site_url": S + "/lt/lamps-c100", "discovery_mode": "crawl", "fetch_mode": "http", "stay_in_section": True,
                                  "url_include": ["-p\\d+$"], **fast(max_concurrency=1)})
    assert cfg["stay_in_section"] is True and cfg["crawl_include"] == []
    run = check(service.get(f"/v1/runs/{service.wait_run(start(service, cfg, 'scope-section').json()['run_id'])['run_id']}"),
                "run_summary.schema.json", 200)
    c = run["counters"]
    assert c["records_extracted"] == 4, c  # lamp a, b (start page), c (subcategory), d (page 2)
    assert c["urls_skipped"].get("scope", 0) >= 3, c["urls_skipped"]  # tools, garden, about from the walked pages
    hits = fixture_world.state(8602)["hits"]
    assert not hits.get("8602/lt/tools-c200") and not hits.get("8602/tool-x-p9") and not hits.get("8602/about-us"), hits
    assert hits.get("8602/lt/lamps-c100/pendant-c101") == 1
    # robots.txt advertises a valid index. Explicit category crawling still
    # follows pagination and outside-directory products without reading it.
    assert hits.get("8602/robots.txt")
    assert not any("sitemap" in path and count for path, count in hits.items()), hits
    assert run.get("sitemap_discovery") is None


def test_without_a_scope_the_crawl_leaves_the_category(service, fixture_world):
    ensure_mappings(service, SITE, ITEM)
    fixture_world.set(8602, reset_hits=True)
    cfg = merged_config(service, {"site_url": S + "/lt/lamps-c100", "discovery_mode": "crawl", "fetch_mode": "http",
                                  "url_include": ["-p\\d+$"], **fast(max_concurrency=1)})
    run = service.wait_run(start(service, cfg, "scope-none").json()["run_id"])
    assert run["counters"]["records_extracted"] == 6 and not run["counters"]["urls_skipped"].get("scope"), run["counters"]


def test_crawl_include_validates(service):
    body = service.post("/v1/config/validate", {"run_inputs": {"site_url": S + "/", "crawl_include": ["("]}, "profile": profile()}).json()
    assert body["valid"] is False and body["errors"][0]["field"] == "crawl_include[0]" and body["errors"][0]["code"] == "pattern_invalid"


def test_single_scoped_sitemap_run_uses_explicit_raw_budget_and_one_export(service, fixture_world):
    import json

    # A literal loopback origin exercises the real native HTTP transport on
    # developer hosts too, without operating-system .test DNS configuration.
    S = "http://127.0.0.1:8602"
    ensure_mappings(service, "127.0.0.1:8602", ITEM)
    fixture_world.set(8602, reset_hits=True)
    cfg = merged_config(service, {
        "site_url": S + "/lt/lamps-c100", "discovery_mode": "sitemap", "fetch_mode": "http", "browser": "auto",
        "sitemap_url": S + "/scope-map-index.xml",
        "sitemap_source_include": [r"/scope-map-products\.xml$"],
        "url_include": [r"/lamp-[a-z]-p\d+$"], "max_sitemap_entries": 8,
        **fast(max_concurrency=1),
    })
    run = service.wait_run(start(service, cfg, "scoped-source-full-single-run").json()["run_id"])
    assert run["state"] == "completed" and run["outcome"] == "complete", run
    report = run["sitemap_discovery"]
    assert report["complete"] and report["entries_seen"] == 8
    assert report["scope"] == "configured_sources" and report["excluded_source_count"] == 1
    records = [json.loads(line) for line in service.get(run["result"]["download_path"]).text.splitlines()]
    assert {record["source_url"] for record in records} == {S + "/lamp-a-p1", S + "/lamp-b-p2"}
    assert run["result"]["record_count"] == 2
    assert not run.get("recollect_of") and not run.get("recollected_by")
    hits = fixture_world.state(8602)["hits"]
    assert hits.get("8602/scope-map-index.xml") == 1
    assert hits.get("8602/scope-map-products.xml") == 1
    assert not hits.get("8602/scope-map-tools.xml")
    assert not any("irrelevant" in path and count for path, count in hits.items()), hits
