"""Unusable 200 answers, page timeout and the automatic slow-down, politeness presets,
a start while the site is busy — on the fixture shop's /jk/ catalogue (variants.test)."""

from __future__ import annotations

import time

from tests.conftest import check, ensure_mappings, err, fast, merged_config, start

V = "http://variants.test:8602"
SITE = "variants.test:8602"
JK = {"schema_version": 1, "template_id": "jk", "match": {"url_patterns": ["/jk/p[0-9]+($|[?])"]}, "fields": [
    {"name": "title", "type": "text", "required": True, "sources": [{"kind": "css", "selector": "h1.name", "extract": "text"}]},
    {"name": "price", "type": "price", "sources": [{"kind": "css", "selector": ".price", "extract": "text"}]}]}


def _crawl(service, key: str, **extra) -> dict:
    cfg = merged_config(service, {"site_url": f"{V}/jk/", "discovery_mode": "crawl", "fetch_mode": "http", **fast(max_concurrency=1), **extra})
    r = start(service, cfg, key)
    assert r.status_code == 201, r.text
    return service.wait_run(r.json()["run_id"])


def test_listing_and_products_with_transient_junk_lose_nothing(service, fixture_world):
    """A maintenance page on the listing, an empty and a cut-off product page (all status 200): retried, nothing lost."""
    ensure_mappings(service, SITE, JK)
    fixture_world.set(8602, reset_hits=True, jk_junk={"/jk/": ["maintenance"], "/jk/p3": ["empty"], "/jk/p5": ["truncated", "truncated"]},
                      jk_fail_every=0)
    run = _crawl(service, "jk-junk")
    c = run["counters"]
    assert run["state"] == "completed" and c["records_extracted"] == 6, c
    assert c["soft_errors"] >= 4 and {"error_page", "empty", "truncated"} <= set(c["soft_errors_by_reason"]), (c["soft_errors_by_reason"], run["timeline"])
    assert c["repairs_started"] == 0 and run["outcome"] == "complete"
    assert any(e["kind"] == "soft_error" for e in run["timeline"]), run["timeline"]
    assert run["politeness"]["preset"] == "fast" and run["politeness"]["text"].startswith("fast: 0–0 s"), run["politeness"]


def test_junk_that_stays_ends_as_fetch_failed_not_incomplete(service, fixture_world):
    ensure_mappings(service, SITE, JK)
    fixture_world.set(8602, reset_hits=True, jk_junk={"/jk/p2": ["maintenance"] * 5}, jk_fail_every=0)
    run = _crawl(service, "jk-junk-stays", max_retries=1)
    assert run["counters"]["records_extracted"] == 5 and run["counters"]["repairs_started"] == 0, run["counters"]
    items = service.get(f"/v1/runs/{run['run_id']}/unextracted", params={"limit": 50}).json()["items"]
    assert [(i["url"].rsplit("/", 1)[-1], i["reason"]) for i in items] == [("p2", "fetch_failed")], items
    assert items[0]["last_error"].startswith("unusable answer: an error or maintenance page")


def test_partial_pages_are_fetched_again_before_a_repair(service, fixture_world):
    """Product pages missing the price on their first answer (a normal-looking page) would open a repair;
    fetched again they are fine, so no repair is asked for."""
    ensure_mappings(service, SITE, JK)
    fixture_world.set(8602, reset_hits=True, jk_junk={"/jk/p2": ["partial"], "/jk/p3": ["partial"], "/jk/p4": ["partial"]}, jk_fail_every=0)
    run = _crawl(service, "jk-partial", required_fields=["title", "price"])
    c = run["counters"]
    assert run["state"] == "completed" and c["records_extracted"] == 6 and c["repairs_started"] == 0, c
    assert c["repairs_avoided"] >= 1 and any(e["kind"] == "repair_check" for e in run["timeline"]), run["timeline"]
    assert not [e for e in run["events"] if e["event_type"] == "mapping_required"]


def test_server_errors_slow_the_host_down(service, fixture_world):
    ensure_mappings(service, SITE, JK)
    fixture_world.set(8602, reset_hits=True, jk_junk={}, jk_fail_every=3)
    try:
        urls = [{"url": f"{V}/jk/p{i}?n={k}"} for k in range(4) for i in range(1, 7)]
        cfg = merged_config(service, {"discovery_mode": "url_list", "urls": urls, "fetch_mode": "http", "max_retries": 3, **fast(max_concurrency=1)})
        run = service.wait_run(start(service, cfg, "jk-slow").json()["run_id"], timeout=400)
        slowed = [e for e in run["timeline"] if e["kind"] == "slowed_down"]
        assert slowed and "errors in the last" in slowed[0]["message"] and "variants.test" in slowed[0]["message"], run["timeline"]
        assert "variants.test" in run["politeness"].get("slowed_hosts", {}) or any(e["kind"] == "relaxed" for e in run["timeline"])
    finally:
        fixture_world.set(8602, jk_fail_every=0)


def test_page_timeout(service, fixture_world):
    cfg = merged_config(service, {"discovery_mode": "url_list", "urls": [{"url": f"{V}/slow/hang?s=25"}], "fetch_mode": "http",
                                  "page_timeout_seconds": 10, "max_retries": 0, **fast()})
    t0 = time.monotonic()
    run = service.wait_run(start(service, cfg, "page-timeout").json()["run_id"])
    assert time.monotonic() - t0 < 24, "the page timeout did not cut the 25 s page"
    items = service.get(f"/v1/runs/{run['run_id']}/unextracted").json()["items"]
    assert items and items[0]["reason"] == "fetch_failed", items


def test_politeness_presets_and_explicit_fields(service):
    def validate(run_inputs):
        r = service.post("/v1/config/validate", {"run_inputs": run_inputs, "site_settings": {}, "profile": {"profile_name": "p", "proxy_tiers": [],
                                                 "solver": None, "webhook": {"url": "http://hook.test:8604/hook/x", "signing_secret": "fs_whsec_fixture_0000000000000001"}}})
        assert r.status_code == 200, r.text
        return r.json()["config"]

    base = {"site_url": f"{V}/jk/"}
    c = validate(base)
    assert (c["politeness"], c["delay_min_seconds"], c["delay_max_seconds"], c["max_concurrency"], c["max_requests_per_minute"],
            c["page_timeout_seconds"], c["when_site_busy"]) == ("normal", 2, 5, 2, 30, 60, "skip")
    c = validate({**base, "politeness": "gentle"})
    assert (c["delay_min_seconds"], c["delay_max_seconds"], c["max_concurrency"], c["max_requests_per_minute"], c["page_timeout_seconds"]) == (5, 10, 1, 10, 120)
    c = validate({**base, "politeness": "fast", "max_concurrency": 1, "delay_min_seconds": 3})
    assert (c["delay_min_seconds"], c["delay_max_seconds"], c["max_concurrency"], c["max_requests_per_minute"]) == (3, 3, 1, 120)


def test_a_start_while_the_site_is_busy_is_skipped(service, fixture_world):
    ensure_mappings(service, SITE, JK)
    fixture_world.set(8602, reset_hits=True, jk_junk={}, jk_fail_every=0)
    slow = merged_config(service, {"discovery_mode": "url_list", "urls": [{"url": f"{V}/slow/busy?s=6"}], "fetch_mode": "http", **fast()})
    first = start(service, slow, "busy-1").json()
    try:
        again = merged_config(service, {"site_url": f"{V}/jk/", "fetch_mode": "http", "when_site_busy": "skip", **fast()})
        body = err(start(service, again, "busy-2"), 409, "site_busy")["error"]
        assert body["details"]["run_id"] == first["run_id"] and body["details"]["state"] in ("queued", "running")
        assert body["message"].startswith(f"A run of {SITE} is already") and body["message"].endswith("this start was skipped.")
        assert service.get("/v1/runs", params={"limit": 50}).json()["items"][0]["run_id"] == first["run_id"]  # nothing stored
        queued = merged_config(service, {"site_url": f"{V}/jk/", "fetch_mode": "http", "when_site_busy": "queue", **fast()})
        r = start(service, queued, "busy-3")
        assert r.status_code == 201 and r.json()["state"] == "queued", r.text
        service.wait_run(r.json()["run_id"], timeout=200)
    finally:
        service.wait_run(first["run_id"])
