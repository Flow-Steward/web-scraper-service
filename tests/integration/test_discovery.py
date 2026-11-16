"""Discovery modes, url_list with keys, limits, html fields, overlap, stable record ids."""

from __future__ import annotations

import json
import time

from tests.conftest import (
    PART_MAPPING, PLAIN, PRODUCT_MAPPING, SHOP, SHOP_SITE, check, ensure_mappings, err, fast, merged_config, new_service, profile, start,
)


def _records(svc, run) -> list[dict]:
    data = svc.get(run["result"]["download_path"]).content.decode()
    return [json.loads(line) for line in data.splitlines()]


def _hits(fixture_world, port=8601) -> dict:
    return fixture_world.state(port)["hits"]


def test_sitemap_never_follows_links(service, fixture_world):
    ensure_mappings(service, SHOP_SITE, PRODUCT_MAPPING, PART_MAPPING)
    fixture_world.set(8601, reset_hits=True)
    cfg = merged_config(service, {"site_url": SHOP + "/", "discovery_mode": "sitemap", "fetch_mode": "http", **fast()})
    run = service.wait_run(start(service, cfg, "disc-sitemap").json()["run_id"])
    hits = _hits(fixture_world)
    assert run["state"] == "completed" and run["resolved"]["discovery_mode"] == "sitemap"
    assert not any(k.startswith("8601/kategorie") for k in hits), hits  # links are never followed
    assert hits.get("8601/sitemap-products.xml.gz") and hits.get("8601/sitemap-parts.xml")  # index + gzip
    assert not any(k.startswith("8601/private") for k in hits)  # robots.txt honoured
    c = run["counters"]
    assert c["records_extracted"] == 24 + 8 + 3 - 3  # products + parts; js pages need the browser (http mode)
    reasons = {i["reason"] for i in service.get(f"/v1/runs/{run['run_id']}/unextracted").json()["items"]}
    assert "robots_disallowed" in reasons and "not_found" in reasons
    recs = _records(service, run)
    first = next(r for r in recs if r["source_url"].endswith("/produkt/leuchte-01"))
    assert first["requested_url"].endswith("?utm_source=sitemap")  # canonical vs requested


def test_crawl_never_reads_sitemap_and_html_field(service, fixture_world):
    ensure_mappings(service, "plain.test:8602", PRODUCT_MAPPING, PART_MAPPING)
    fixture_world.set(8602, reset_hits=True)
    cfg = merged_config(service, {"site_url": PLAIN + "/", "discovery_mode": "crawl", "fetch_mode": "http", **fast()})
    run = service.wait_run(start(service, cfg, "disc-crawl").json()["run_id"])
    hits = _hits(fixture_world, 8602)
    assert not any("sitemap" in k for k in hits), hits
    assert run["state"] == "completed" and run["counters"]["records_extracted"] >= 12
    assert run["counters"]["urls_skipped"]["robots"] >= 1  # /private/ from the category page
    rec = next(r for r in _records(service, run) if r["template_id"] == "product")
    html = rec["data"]["description"]
    assert "<p>" in html and "<ul>" in html and "<table>" in html
    assert "<script" not in html and "<style" not in html and "onclick" not in html
    assert set(rec["data"]) == {f["name"] for f in PRODUCT_MAPPING["fields"]}


def test_auto_and_sitemap_and_crawl(service, fixture_world):
    ensure_mappings(service, SHOP_SITE, PRODUCT_MAPPING, PART_MAPPING)
    ensure_mappings(service, "plain.test:8602", PRODUCT_MAPPING, PART_MAPPING)
    cfg = merged_config(service, {"site_url": SHOP + "/", "fetch_mode": "http", "max_pages": 5, **fast()})
    run = service.wait_run(start(service, cfg, "disc-auto-shop").json()["run_id"])
    assert run["resolved"]["discovery_mode"] in ("sitemap", "sitemap_and_crawl") and run["resolved"]["reasons"]
    cfg = merged_config(service, {"site_url": PLAIN + "/", "fetch_mode": "http", "max_pages": 5, **fast()})
    run = service.wait_run(start(service, cfg, "disc-auto-plain").json()["run_id"])
    assert run["resolved"]["discovery_mode"] == "crawl" and "no sitemap" in run["resolved"]["reasons"][0]
    cfg = merged_config(service, {"site_url": SHOP + "/", "discovery_mode": "sitemap_and_crawl", "fetch_mode": "http", **fast()})
    run = service.wait_run(start(service, cfg, "disc-both").json()["run_id"])
    assert run["state"] == "completed" and run["counters"]["urls_skipped"]["duplicate"] > 0  # de-duplicated


def test_url_list_with_keys_fetches_exactly_the_list(service, fixture_world):
    ensure_mappings(service, SHOP_SITE, PRODUCT_MAPPING, PART_MAPPING)
    fixture_world.set(8601, reset_hits=True)
    urls = [
        {"url": f"{SHOP}/produkt/leuchte-05", "key": "A-5"},
        {"url": f"{SHOP}/produkt/leuchte-05", "key": "B-5"},  # one page under two keys -> two records
        {"url": f"{SHOP}/ersatzteile/teil-02", "key": "T-2"},
        {"url": f"{SHOP}/produkt/leuchte-06?utm_campaign=x"},
    ]
    cfg = merged_config(service, {"discovery_mode": "url_list", "urls": urls, "fetch_mode": "http", **fast()})
    run = service.wait_run(start(service, cfg, "disc-list").json()["run_id"])
    hits = {k: v for k, v in _hits(fixture_world).items() if not k.endswith("robots.txt")}
    assert set(hits) == {"8601/produkt/leuchte-05", "8601/ersatzteile/teil-02", "8601/produkt/leuchte-06"}, hits
    recs = _records(service, run)
    assert sorted(r.get("source_key") for r in recs if "source_key" in r) == ["A-5", "B-5", "T-2"]
    assert len({r["record_id"] for r in recs}) == 4
    assert run["outcome"] == "complete" and run["stop_reason"] == "finished"
    assert run["sites"] == [SHOP_SITE]


def test_max_records_and_run_completed(service, fixture_world):
    ensure_mappings(service, SHOP_SITE, PRODUCT_MAPPING, PART_MAPPING)
    cfg = merged_config(service, {"site_url": SHOP + "/", "discovery_mode": "sitemap", "fetch_mode": "http", "max_records": 10, "url_exclude": ["/private/", "/gone/"], **fast(max_concurrency=2)})
    run = service.wait_run(start(service, cfg, "disc-maxrec").json()["run_id"])
    assert run["stop_reason"] == "max_records" and run["state"] == "completed"
    assert run["counters"]["records_extracted"] == 10 and run["result"]["record_count"] == 10  # exact (README 4.8 as amended)
    assert run["outcome"] == "complete"
    events = [e for e in run["events"] if e["event_type"] == "run_completed"]
    assert len(events) == 1
    assert run["progress"]["discovery"] == "cut"


def test_max_pages(service, fixture_world):
    ensure_mappings(service, SHOP_SITE, PRODUCT_MAPPING, PART_MAPPING)
    cfg = merged_config(service, {"site_url": SHOP + "/", "discovery_mode": "sitemap", "fetch_mode": "http", "max_pages": 6, **fast(max_concurrency=1)})
    run = service.wait_run(start(service, cfg, "disc-maxpages").json()["run_id"])
    assert run["stop_reason"] == "max_pages" and 6 <= run["counters"]["pages_fetched"] <= 7


def test_record_ids_identical_across_runs(service, fixture_world):
    ensure_mappings(service, SHOP_SITE, PRODUCT_MAPPING, PART_MAPPING)
    cfg = merged_config(service, {"site_url": SHOP + "/", "discovery_mode": "sitemap", "fetch_mode": "http", "url_include": ["/ersatzteile/"], **fast()})
    a = service.wait_run(start(service, cfg, "disc-stable-a").json()["run_id"])
    b = service.wait_run(start(service, cfg, "disc-stable-b").json()["run_id"])
    ids_a = [r["record_id"] for r in _records(service, a)]
    ids_b = [r["record_id"] for r in _records(service, b)]
    assert ids_a == ids_b and len(ids_a) == 8


def test_discovery_overlaps_fetching(service, fixture_world):
    """Discovery, fetching and extraction overlap: records exist while discovery still runs."""
    ensure_mappings(service, SHOP_SITE, PRODUCT_MAPPING, PART_MAPPING)
    cfg = merged_config(service, {"site_url": SHOP + "/", "discovery_mode": "sitemap_and_crawl", "fetch_mode": "http",
                                  "delay_min_seconds": 0.2, "delay_max_seconds": 0.3, "max_concurrency": 1})
    run_id = start(service, cfg, "disc-overlap").json()["run_id"]
    overlap = False
    deadline = time.time() + 120
    while time.time() < deadline:
        run = service.get(f"/v1/runs/{run_id}").json()
        if run["progress"]["discovery"] == "running" and run["counters"]["records_extracted"] > 0 and run["progress"]["queue_pending"] > 0:
            overlap = True
            break
        if run["state"] == "completed":
            break
        time.sleep(0.2)
    service.post(f"/v1/runs/{run_id}/stop", {})
    service.wait_run(run_id)
    assert overlap


def test_429_and_slow_pages(service, fixture_world):
    ensure_mappings(service, SHOP_SITE, PRODUCT_MAPPING, PART_MAPPING)
    fixture_world.set(8601, reset_hits=True)
    urls = [{"url": f"{SHOP}/flaky/x"}, {"url": f"{SHOP}/slow/y?s=1.5"}]
    cfg = merged_config(service, {"discovery_mode": "url_list", "urls": urls, "fetch_mode": "http", **fast()})
    run = service.wait_run(start(service, cfg, "disc-429").json()["run_id"])
    assert run["counters"]["records_extracted"] == 2 and run["counters"]["retries"] >= 2
    assert fixture_world.state()["hits"]["8601/flaky/x"] == 3


def test_private_targets_refused_without_override(fixture_world):
    svc = new_service(8091, WSS_UNSAFE_ALLOW_PRIVATE_TARGETS="0").start()
    try:
        body = svc.post("/v1/config/validate", {"run_inputs": {"site_url": "http://127.0.0.1:8601/"}, "profile": profile()}).json()
        assert body["valid"] is False and body["errors"][0]["code"] == "private_address_refused"
        body = svc.post("/v1/config/validate", {"run_inputs": {"discovery_mode": "url_list", "urls": [{"url": "http://localhost:8601/a"}, {"url": "http://10.1.2.3/b"}]}, "profile": profile()}).json()
        assert [e["field"] for e in body["errors"]] == ["urls[0].url", "urls[1].url"]
        cfg = body.get("config")
        assert cfg is None
        err(svc.post("/v1/analyze", {"config": {**_defaults(svc), "site_url": "http://127.0.0.1:8601/"}, "profile": profile()}), 422, "validation_failed")
    finally:
        svc.terminate()


def _defaults(svc) -> dict:
    return svc.post("/v1/config/validate", {"run_inputs": {"site_url": "https://example.com/"}, "profile": profile()}).json()["config"]


def test_max_duration(service, fixture_world):
    ensure_mappings(service, SHOP_SITE, PRODUCT_MAPPING, PART_MAPPING)
    urls = [{"url": f"{SHOP}/slow/s{i}?s=3"} for i in range(40)]
    cfg = merged_config(service, {"discovery_mode": "url_list", "urls": urls, "fetch_mode": "http", "max_duration_minutes": 1, **fast(max_concurrency=1)})
    run = service.wait_run(start(service, cfg, "disc-duration").json()["run_id"], timeout=180)
    assert run["stop_reason"] == "max_duration" and run["outcome"] == "partial"
    assert run["counters"]["pages_fetched"] < 40
    reasons = {i["reason"] for i in service.get(f"/v1/runs/{run['run_id']}/unextracted", params={"limit": 200}).json()["items"]}
    assert reasons == {"stopped"}  # listed URLs not fetched before the limit
