"""Samples on a protected shop ran ~4 minutes and answered only "no
target page". Reproduced with fixtures: a guarded category whose first 30 links are the menu
(product cards after them), and a site-wide sitemap whose first entries are not products."""

from __future__ import annotations

from tests.conftest import GUARD, check, merged_config, profile

CATEGORY = GUARD + "/kategorie/lampen"


def test_category_crawl_with_selector_samples_product_cards_in_the_cleared_tab(guarded_service, fixture_world):
    fixture_world.set(8603, guard_mode="shadow", guard_ttl=1000, reset_clearances=True)
    cfg = merged_config(guarded_service, {"site_url": CATEGORY, "discovery_mode": "crawl", "target_page_selector": "div.product-detail",
                                          "browser": "chromium", "challenge_click": True, "sample_count": 2,
                                          "delay_min_seconds": 0, "delay_max_seconds": 0})
    body = check(guarded_service.post("/v1/samples", {"config": cfg, "profile": profile()}), "samples.schema.json", 200)
    assert len(body["samples"]) == 2 and all("/p/" in s["url"] for s in body["samples"]), body.get("explanation")
    ex = body["explanation"]
    assert ex["discovery"]["source"] == "crawl" and ex["discovery"]["links"] >= 37, ex["discovery"]
    tried = [p["url"] for p in ex["pages"] if p["outcome"] != "start"]
    assert tried and all("/p/" in u for u in tried), tried  # product cards before the 30 menu links and the pagination link
    start = ex["discovery"]["start_page"]
    assert start["variants_skipped"] == 1 and {"shape": "/p/<n>", "links": 6, "in_menus": 0.0} in start["shapes"], start
    assert ex["counts"]["samples"] == 2 and ex["counts"]["http"] == 0, ex["counts"]  # no HTTP probe after the challenge
    # the challenged host's pages went only to the cleared tab: every product load came from the browser (Referer set)
    loads = [x for x in fixture_world.state(8603)["guard_loads"] if x["path"].startswith("/p/")]
    assert loads and all(x["referer"] for x in loads), loads
    assert "samples site=guard.test:8603 source=crawl" in guarded_service.log()


def test_selector_that_matches_nothing_is_explained_with_a_hint(guarded_service, fixture_world):
    fixture_world.set(8603, guard_mode="shadow", guard_ttl=1000, reset_clearances=True)
    cfg = merged_config(guarded_service, {"site_url": CATEGORY, "discovery_mode": "crawl", "target_page_selector": ".product-page__content",
                                          "browser": "chromium", "challenge_click": True, "sample_count": 1,
                                          "delay_min_seconds": 0, "delay_max_seconds": 0})
    body = check(guarded_service.post("/v1/samples", {"config": cfg, "profile": profile()}), "samples.schema.json", 200)
    assert body["samples"] == []
    ex = body["explanation"]
    # Generic traversal may reveal new shapes through product/category links.
    # It must remain within the sample plan's budget and account for every URL,
    # including the fixture's linked missing pages rather than hiding their errors.
    counts = ex["counts"]
    assert 0 < counts["tried"] <= max(cfg["sample_count"] * 6, 20), counts
    assert counts["tried"] == counts["not_target"] + counts["failed"] and counts["selector_found"] == 0, counts
    tried = [p for p in ex["pages"] if p["outcome"] != "start"]
    assert len({p["url"] for p in tried}) == len(tried) and all(p["url"] != CATEGORY for p in tried), tried
    rejected = [p for p in tried if p["outcome"] == "not_target"]
    assert rejected and {p["reason"] for p in rejected} == {"target_page_selector absent"}
    assert all(p["selector_found"] is False and p["status"] == 200 and "links" in p for p in rejected), rejected
    failed = [p for p in tried if p["outcome"] == "failed"]
    assert all(p["status"] == 404 and p["reason"] == "HTTP 404" for p in failed), failed
    kinds = [h["kind"] for h in ex["hints"]]
    assert kinds[0] == "target_page_selector" and ".product-page__content" in ex["hints"][0]["message"], ex["hints"]
    assert "url_include" in kinds  # product pages (price, gallery …) were seen: a pattern is offered
    warn = next(w for w in body["warnings"] if w["field"] == "sample_count")
    assert warn["message"].startswith("found 0 of 1 target page: start page (browser, HTTP 200): 38 links, 37 on this site in ")
    loaded = len(rejected)
    assert f"Next: none of the {loaded} pages loaded had the product page selector" in warn["message"], warn
    assert f"samples site=guard.test:8603 source=crawl start_links=38/37 shapes={ex['discovery']['shapes']}" in guarded_service.log()
    assert "/p/11 browser 200 sel=no links=" in guarded_service.log()


def test_sitewide_sitemap_is_sampled_across_url_shapes(service, fixture_world):
    cfg = merged_config(service, {"site_url": "http://bigmap.test:8602/", "fetch_mode": "http", "sample_count": 3,
                                  "delay_min_seconds": 0, "delay_max_seconds": 0})
    body = check(service.post("/v1/samples", {"config": cfg, "profile": profile()}), "samples.schema.json", 200)
    assert len(body["samples"]) == 3 and all("/produkt/" in s["url"] for s in body["samples"]), body["explanation"]["summary"]
    ex = body["explanation"]
    assert ex["discovery"]["source"] == "sitemap" and ex["discovery"]["sitemap_urls"] == 94 and "guess" not in ex, ex["discovery"]
    shapes = {g["shape"] for g in ex["groups"]}
    assert {"/seite/*-<n>", "/produkt/*-<n>"} <= shapes, ex["groups"]
    assert ex["counts"]["tried"] <= 5, ex["counts"]  # one per shape first, then the shape that gave a sample
    assert ex["summary"].startswith("sitemap: 94 URLs; probed: /seite/*-<n> 1")


def test_mega_menu_category_finds_products_in_a_few_loads(guarded_service, fixture_world):
    """A protected category page in miniature: 200 menu category links (a category tree) come first in the HTML,
    12 product links last; plain links, no cards or nav markup. One link per URL shape first."""
    fixture_world.set(8603, guard_mode="shadow", guard_ttl=1000, reset_clearances=True)
    cfg = merged_config(guarded_service, {"site_url": GUARD + "/zibintai-c1331", "discovery_mode": "crawl",
                                          "target_page_selector": ".product-page__content", "browser": "chromium", "challenge_click": True,
                                          "sample_count": 2, "delay_min_seconds": 0, "delay_max_seconds": 0})
    body = check(guarded_service.post("/v1/samples", {"config": cfg, "profile": profile()}), "samples.schema.json", 200)
    assert len(body["samples"]) == 2 and all(s["url"].rsplit("-p", 1)[-1].isdigit() for s in body["samples"]), body["explanation"]["summary"]
    ex = body["explanation"]
    assert ex["counts"]["tried"] <= 4, ex["pages"]  # /ru/*-c<n> (5 links) once, then the product shape
    start = ex["discovery"]["start_page"]
    assert start["hrefs"] == 220 and start["in_site"] == 217 and start["other_hosts"] == 1 and start["variants_skipped"] == 2, start
    shapes = {g["shape"]: g["links"] for g in start["shapes"]}
    assert shapes == {"/*-c<n>": 200, "/*-p<n>": 12, "/ru/*-c<n>": 5}, shapes
    assert "/*-c<n> ×200, /*-p<n> ×12, /ru/*-c<n> ×5" in ex["summary"], ex["summary"]
    assert not [p for p in ex["pages"] if p["outcome"] != "start" and p["shape"] == "/*-c<n>"]  # never wandered into the menu


LEMPA = {"schema_version": 1, "template_id": "lempa", "label": "Lempa", "match": {"url_patterns": ["-p\\d+$"]},
         "fields": [{"name": "title", "type": "text", "required": True, "sources": [{"kind": "css", "selector": "h1", "extract": "text"}]}]}


def test_job_crawls_target_links_first_from_a_mega_menu_category(guarded_service, fixture_world):
    """A job from such a category fetched info/footer pages and other categories in
    document order (the first 2000 hrefs) and never reached a product."""
    from tests.conftest import ensure_mappings, fast, start

    fixture_world.set(8603, guard_mode="shadow", guard_ttl=1000, reset_clearances=True)
    ensure_mappings(guarded_service, "guard.test:8603", LEMPA)
    cfg = merged_config(guarded_service, {"site_url": GUARD + "/zibintai-c1331", "discovery_mode": "crawl", "fetch_mode": "browser",
                                          "url_include": ["-p[0-9]+$"], "target_page_selector": ".product-page__content", "browser": "chromium",
                                          "challenge_click": True, "max_records": 3, **fast(max_concurrency=1)})
    run = guarded_service.wait_run(start(guarded_service, cfg, "a35-job-order").json()["run_id"])
    c = run["counters"]
    assert c["records_extracted"] == 3 and c["pages_fetched"] <= 6, c
    loads = [x["path"] for x in fixture_world.state(8603)["guard_loads"] if x["path"] != "/zibintai-c1331"]
    assert loads[:3] and all(p.rsplit("-p", 1)[-1].isdigit() for p in loads[:3]), loads


def test_job_keeps_one_context_per_session(guarded_service, fixture_world):
    """A cleared session was challenged again every few pages — a fresh incognito
    context per page. The guarded pages count their visits in localStorage: one context per
    session counts up."""
    from tests.conftest import ensure_mappings, fast, start
    from tests.integration.test_click_route import GM

    fixture_world.set(8603, guard_mode="shadow", guard_ttl=1000, reset_clearances=True)
    ensure_mappings(guarded_service, "guard.test:8603", GM)
    cfg = merged_config(guarded_service, {"discovery_mode": "url_list", "urls": [{"url": f"{GUARD}/p/{i}"} for i in range(21, 25)],
                                          "challenge_click": True, "browser": "chromium", **fast(max_concurrency=1)})
    run = guarded_service.wait_run(start(guarded_service, cfg, "a35-one-context").json()["run_id"])
    assert run["counters"]["records_extracted"] == 4 and run["protection"]["challenges_met"] == 1, (run["counters"], run["protection"])
    counts = fixture_world.state(8603)["guard_storage"]
    assert max(counts) >= 3, counts  # the same localStorage across the session's pages


def test_every_fetch_failed_page_has_a_reason_in_errors(service, fixture_world):
    """A fetch_failed page had nothing in errors[]."""
    from tests.conftest import PRODUCT_MAPPING, SHOP, SHOP_SITE, ensure_mappings, fast, start

    ensure_mappings(service, SHOP_SITE, PRODUCT_MAPPING)
    urls = [f"{SHOP}/produkt/leuchte-01", f"{SHOP}/gone/old-lamp", "http://shop.test:8699/produkt/x"]
    cfg = merged_config(service, {"discovery_mode": "url_list", "urls": [{"url": u} for u in urls], "fetch_mode": "http", "max_retries": 0,
                                  **fast(max_concurrency=1)})
    run = check(service.get(f"/v1/runs/{service.wait_run(start(service, cfg, 'a37-fetch-failed').json()['run_id'])['run_id']}"),
                "run_summary.schema.json", 200)
    failed = {e["url"]: e for e in run["errors"] if e["code"] == "page_fetch_failed"}
    assert set(failed) == {f"{SHOP}/gone/old-lamp", "http://shop.test:8699/produkt/x"}, run["errors"]
    assert "HTTP 404" in failed[f"{SHOP}/gone/old-lamp"]["message"] and failed[f"{SHOP}/gone/old-lamp"]["severity"] == "warning"
    assert "shop.test:8699/produkt/x: " in failed["http://shop.test:8699/produkt/x"]["message"]
