"""Integration tests for the review fixes (service review, 2026-11-13).

Each test failed (hung, or asserted otherwise) before its fix.
"""

from __future__ import annotations

import copy
import json
import sqlite3
import time

import pytest

from tests.conftest import (
    GUARD, HOOK, PRODUCT_MAPPING, SECRETS_KEY, SHOP, SHOP_SITE, check, ensure_mappings, err, fast, merged_config, new_service, profile, start,
)

TIERS = [
    {"tier": 1, "provider": "generic", "protocol": "http", "host": "127.0.0.1", "port": 8606, "auth_mode": "user_password",
     "username": "tier1user", "password": "tier1pass", "session_mode": "sticky", "sticky_minutes": 10},
    {"tier": 2, "provider": "generic", "protocol": "http", "host": "127.0.0.1", "port": 8607, "auth_mode": "user_password",
     "username": "tier2user", "password": "tier2pass", "session_mode": "sticky", "sticky_minutes": 10},
]
GM = copy.deepcopy(PRODUCT_MAPPING)
GM["match"] = {"url_patterns": ["/p/\\d+$"]}


def _wait(pred, timeout=90, what=""):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = pred()
        if value:
            return value
        time.sleep(0.3)
    raise AssertionError(f"timeout waiting for {what}")


def _runtime(svc, run_id) -> dict:
    db = sqlite3.connect(f"{svc.data_dir}/service.sqlite3")
    try:
        return json.loads(db.execute("SELECT runtime FROM runs WHERE run_id=?", (run_id,)).fetchone()[0])
    finally:
        db.close()


@pytest.fixture(scope="module")
def rsvc(fixture_world):
    s = new_service(8096, WSS_LIMIT_MAX_CONCURRENT_JOBS="1").start()
    yield s
    s.terminate()


# ---------------------------------------------------------------- shutdown and restart
def test_b1_restart_after_operator_stop_completes(rsvc, fixture_world):
    ensure_mappings(rsvc, SHOP_SITE, PRODUCT_MAPPING)
    urls = [{"url": f"{SHOP}/slow/b1stop{i}?s=4"} for i in range(6)]
    cfg = merged_config(rsvc, {"discovery_mode": "url_list", "urls": urls, "fetch_mode": "http", **fast(max_concurrency=1)})
    run_id = start(rsvc, cfg, "b1-stop").json()["run_id"]
    _wait(lambda: rsvc.get(f"/v1/runs/{run_id}").json()["progress"]["in_progress"] >= 1, what="a page in progress")
    rsvc.post(f"/v1/runs/{run_id}/stop", {})
    _wait(lambda: _runtime(rsvc, run_id).get("stop_handled"), what="stop handled and persisted")
    rsvc.kill()
    rsvc.start()
    run = rsvc.wait_run(run_id, timeout=60)
    assert run["state"] == "completed" and run["stop_reason"] == "stopped"
    assert run["counters"]["pages_fetched"] <= 1  # nothing queued was fetched after the restart


def test_b1_restart_after_limit_cut_completes(rsvc, fixture_world):
    ensure_mappings(rsvc, SHOP_SITE, PRODUCT_MAPPING)
    fixture_world.set(8601, reset_hits=True)
    urls = [{"url": f"{SHOP}/produkt/leuchte-0{i}"} for i in (1, 2)] + [{"url": f"{SHOP}/slow/b1cut{i}?s=8"} for i in range(5)]
    cfg = merged_config(rsvc, {"discovery_mode": "url_list", "urls": urls, "fetch_mode": "http", "max_records": 1, **fast(max_concurrency=2)})
    run_id = start(rsvc, cfg, "b1-cut").json()["run_id"]
    _wait(lambda: _runtime(rsvc, run_id).get("stop_reason") == "max_records", what="the max_records cut")
    rsvc.kill()  # a slow page is still in progress
    rsvc.start()
    run = rsvc.wait_run(run_id, timeout=60)
    assert run["state"] == "completed" and run["stop_reason"] == "max_records"
    slow_hits = sum(v for k, v in fixture_world.state()["hits"].items() if "/slow/b1cut" in k)
    assert slow_hits <= 2  # the queue is not crawled after the restart


# ---------------------------------------------------------------- stopped URLs
def test_m1_queued_job_with_changed_key_waits_for_credentials(rsvc, fixture_world):
    ensure_mappings(rsvc, SHOP_SITE, PRODUCT_MAPPING)
    prof = profile("m1", proxy_tiers=TIERS[:1])
    slow = [{"url": f"{SHOP}/slow/m1a{i}?s=3"} for i in range(8)]
    a = start(rsvc, merged_config(rsvc, {"discovery_mode": "url_list", "urls": slow, "fetch_mode": "http", **fast(max_concurrency=1)}, prof=prof), "m1-a", prof=prof).json()["run_id"]
    b = start(rsvc, merged_config(rsvc, {"discovery_mode": "url_list", "urls": [{"url": f"{SHOP}/produkt/leuchte-03"}], "fetch_mode": "http", **fast()}, prof=prof), "m1-b", prof=prof).json()["run_id"]
    _wait(lambda: rsvc.get(f"/v1/runs/{a}").json()["state"] == "running", what="a running")
    assert rsvc.get(f"/v1/runs/{b}").json()["state"] == "queued"
    rsvc.kill()
    rsvc.start(WSS_SECRETS_KEY="another-secrets-key-0000000000")
    _wait(lambda: rsvc.get(f"/v1/runs/{b}").json()["state"] != "queued", what="b promoted")
    time.sleep(1.5)
    run_b = check(rsvc.get(f"/v1/runs/{b}"), "run_summary.schema.json", 200)
    assert run_b["state"] == "waiting_for_credentials", run_b.get("error")
    assert not [e for e in run_b["events"] if e["event_type"] == "run_failed"]
    for run_id in (a, b):
        rsvc.post(f"/v1/runs/{run_id}/stop", {})
        rsvc.wait_run(run_id, timeout=60)
    rsvc.kill()
    rsvc.start(WSS_SECRETS_KEY=SECRETS_KEY)


def test_minor9_stopping_a_queued_url_list_lists_its_urls(rsvc, fixture_world):
    ensure_mappings(rsvc, SHOP_SITE, PRODUCT_MAPPING)
    slow = [{"url": f"{SHOP}/slow/q{i}?s=3"} for i in range(4)]
    a = start(rsvc, merged_config(rsvc, {"discovery_mode": "url_list", "urls": slow, "fetch_mode": "http", **fast(max_concurrency=1)}), "q-a").json()["run_id"]
    urls = [{"url": f"http://queue.test:8601/produkt/leuchte-0{i}"} for i in range(1, 4)]
    b = start(rsvc, merged_config(rsvc, {"discovery_mode": "url_list", "urls": urls, "fetch_mode": "http", **fast()}), "q-b").json()["run_id"]
    assert rsvc.get(f"/v1/runs/{b}").json()["state"] == "queued"
    run = rsvc.post(f"/v1/runs/{b}/stop", {}).json()
    assert run["state"] == "completed" and run["stop_reason"] == "stopped"
    items = rsvc.get(f"/v1/runs/{b}/unextracted").json()["items"]
    assert sorted(i["url"] for i in items) == sorted(u["url"] for u in urls) and {i["reason"] for i in items} == {"stopped"}
    rsvc.post(f"/v1/runs/{a}/stop", {})
    rsvc.wait_run(a, timeout=60)


# ---------------------------------------------------------------- refetched stored pages
def test_b3_refetch_pending_cleared_when_the_crawl_is_cut(fixture_world):
    from tests.integration.test_repair import REPAIRED

    s = new_service(8097, WSS_LIMIT_MAX_STORED_PAGES_PER_JOB="1").start()
    try:
        site = "repair2.test:8601"
        ensure_mappings(s, site, PRODUCT_MAPPING)
        fixture_world.set(8601, reset_hits=True, layout=1, layout_switch_after=3)
        urls = [{"url": f"http://repair2.test:8601/produkt/leuchte-{i:02d}"} for i in range(1, 13)]
        cfg = merged_config(s, {"discovery_mode": "url_list", "urls": urls, "fetch_mode": "http", "required_fields": ["title", "price"],
                                "max_pages": 14, **fast(max_concurrency=1)})  # 12 pages + 1 confirming refetch before the repair + 1
        run_id = start(s, cfg, "b3-refetch").json()["run_id"]
        run = _wait(lambda: (r := s.get(f"/v1/runs/{run_id}").json())["progress"]["open_mapping_requests"] == 1
                    and r["progress"]["queue_pending"] == 0 and r["progress"]["in_progress"] == 0 and r, timeout=120, what="open request, queue drained")
        assert run["counters"]["refetch_after_repair"] >= 2
        mrq_id = next(e for e in run["events"] if e["event_type"] == "mapping_required")["resource_id"]
        mrq = s.get(f"/v1/mapping-requests/{mrq_id}").json()
        assert mrq["instructions"]["max_refinements"] == 2
        s.put(f"/v1/sites/{site}/mappings", {"template_id": "product", "expected_version": mrq["expected_version"], "mapping": REPAIRED})
        run = s.wait_run(run_id, timeout=90)  # hung forever before the fix (pending_reextraction > 0)
        assert run["state"] == "completed" and run["stop_reason"] == "max_pages" and run["progress"]["pending_reextraction"] == 0
        reasons = {i["reason"] for i in s.get(f"/v1/runs/{run_id}/unextracted", params={"limit": 200}).json()["items"]}
        assert "stopped" in reasons
    finally:
        fixture_world.set(8601, layout=1, layout_switch_after=None)
        s.terminate()


# ---------------------------------------------------------------- max_records exact
@pytest.mark.parametrize("mode,limit,concurrency", [("sitemap", 10, 4), ("crawl", 3, 4)])
def test_max_records_is_exact(service, fixture_world, mode, limit, concurrency):
    ensure_mappings(service, SHOP_SITE, PRODUCT_MAPPING)
    cfg = merged_config(service, {"site_url": SHOP + "/", "discovery_mode": mode, "fetch_mode": "http", "max_records": limit,
                                  "url_exclude": ["/private/", "/gone/"], **fast(max_concurrency=concurrency)})
    run = service.wait_run(start(service, cfg, f"maxrec-exact-{mode}").json()["run_id"])
    lines = service.get(run["result"]["download_path"]).text.splitlines()
    assert len(lines) == limit and run["counters"]["records_extracted"] == limit and run["result"]["record_count"] == limit
    ids = [json.loads(line)["record_id"] for line in lines]
    assert ids == sorted(ids)
    assert run["stop_reason"] == "max_records" and run["outcome"] == "complete"


# ---------------------------------------------------------------- detection
def test_m2_normal_cloudflare_pages_are_extracted(service, fixture_world):
    ensure_mappings(service, SHOP_SITE, PRODUCT_MAPPING)
    urls = [{"url": f"{SHOP}/cf/leuchte-0{i}"} for i in (1, 2, 3)]
    cfg = merged_config(service, {"discovery_mode": "url_list", "urls": urls, "browser": "chromium", **fast(max_concurrency=1)})
    run = service.wait_run(start(service, cfg, "m2-cf-normal").json()["run_id"])
    assert run["counters"]["records_extracted"] == 3, run["counters"]
    assert run["protection"]["challenges_met"] == 0 and run["protection"]["hard_blocks"] == 0
    assert run["counters"]["http_fetches"] == 3 and run["outcome"] == "complete"


# ---------------------------------------------------------------- (auto-mode escalation)
def test_m3_block_rule_uses_the_fetched_host(service, fixture_world):
    prof = profile(proxy_tiers=TIERS)
    urls = [{"url": f"http://www.shop.test:8601/blocked/{i}"} for i in range(1, 9)]
    cfg = merged_config(service, {"discovery_mode": "url_list", "urls": urls, "fetch_mode": "http", **fast(max_concurrency=1)}, prof=prof)
    run = service.wait_run(start(service, cfg, "m3-www-blocked", prof=prof).json()["run_id"])
    assert run["stop_reason"] == "blocked", (run["stop_reason"], run["counters"])
    assert run["counters"]["current_proxy_tier"] == 2 and run["counters"]["pages_fetched"] < 8


def test_m4_auto_mode_escalates_without_a_browser_rerun(service, fixture_world):
    ensure_mappings(service, SHOP_SITE, PRODUCT_MAPPING)
    prof = profile(proxy_tiers=TIERS)
    cfg = merged_config(service, {"discovery_mode": "url_list", "urls": [{"url": f"{SHOP}/tiered/{i}"} for i in range(1, 4)],
                                  "fetch_mode": "auto", "browser": "chromium", **fast(max_concurrency=1)}, prof=prof)
    run = service.wait_run(start(service, cfg, "m4-auto-tier", prof=prof).json()["run_id"])
    c, p = run["counters"], run["protection"]
    assert c["records_extracted"] == 3 and c["browser_fetches"] == 0, (c, p)
    assert p["hard_blocks"] == 1 and p["tier_escalations"] == 1 and p["sessions_retired"] >= 1
    # a hard block is not a challenge
    assert p["challenges_met"] == 0 and p["passed_by_step"] == {}


# ---------------------------------------------------------------- wait before the next tier
def test_c4_wait_comes_before_the_next_tier(service, fixture_world):
    fixture_world.set(8603, guard_mode="auto", guard_ttl=1000, reset_clearances=True)
    ensure_mappings(service, "guard.test:8603", GM)
    prof = profile(proxy_tiers=TIERS)
    cfg = merged_config(service, {"discovery_mode": "url_list", "urls": [{"url": f"{GUARD}/p/{i}"} for i in (1, 2, 3)],
                                  "browser": "chromium", **fast(max_concurrency=1)}, prof=prof)
    run = service.wait_run(start(service, cfg, "c4-wait-first", prof=prof).json()["run_id"])
    p = run["protection"]
    assert run["counters"]["records_extracted"] == 3 and p["passed_by_step"] == {"wait": 1}, (run["counters"], p)
    assert "2" not in run["counters"]["proxy_requests_by_tier"]


# ---------------------------------------------------------------- retries
def test_s2_429_does_not_use_up_max_retries(service, fixture_world):
    ensure_mappings(service, SHOP_SITE, PRODUCT_MAPPING)
    fixture_world.set(8601, reset_hits=True)
    cfg = merged_config(service, {"discovery_mode": "url_list", "urls": [{"url": f"{SHOP}/flaky/s2"}], "fetch_mode": "http", "max_retries": 1, **fast()})
    run = service.wait_run(start(service, cfg, "s2-429").json()["run_id"])
    assert run["counters"]["records_extracted"] == 1, run["counters"]


def test_s3_5xx_is_retried_without_the_browser(service, fixture_world):
    ensure_mappings(service, SHOP_SITE, PRODUCT_MAPPING)
    fixture_world.set(8601, reset_hits=True)
    cfg = merged_config(service, {"discovery_mode": "url_list", "urls": [{"url": f"{SHOP}/err5/leuchte-03"}], "browser": "chromium", "max_retries": 3, **fast()})
    run = service.wait_run(start(service, cfg, "s3-5xx").json()["run_id"])
    assert run["counters"]["records_extracted"] == 1 and run["counters"]["browser_fetches"] == 0
    assert fixture_world.state()["hits"]["8601/err5/leuchte-03"] == 3  # 503, 503, 200 — no browser rerun of a 503


# ---------------------------------------------------------------- discovery through the challenge route
def test_m8_robots_and_sitemap_behind_a_challenge(service, fixture_world):
    fixture_world.set(8603, guard_mode="auto", guard_ttl=1000, reset_clearances=True, guard_discovery=True, reset_hits=True)
    try:
        ensure_mappings(service, "guard.test:8603", GM)
        cfg = merged_config(service, {"site_url": GUARD + "/p/1", "discovery_mode": "sitemap", "browser": "chromium", **fast(max_concurrency=1)})
        run = service.wait_run(start(service, cfg, "m8-guarded-sitemap").json()["run_id"])
        assert run["counters"]["records_extracted"] == 3, (run["counters"], run["protection"], run["resolved"])
        assert fixture_world.state(8603)["hits"].get("8603/guarded-sitemap.xml", 0) >= 1
    finally:
        fixture_world.set(8603, guard_discovery=False)


# ---------------------------------------------------------------- and the webhook exemption
@pytest.fixture(scope="module")
def denied_svc(fixture_world):
    s = new_service(8098, WSS_DENIED_TARGET_HOSTS="internal.test,hook.test").start()
    yield s
    s.terminate()


def test_m7_browser_js_redirect_to_internal_host_is_refused(denied_svc, fixture_world):
    fixture_world.set(8601, reset_hits=True)
    cfg = merged_config(denied_svc, {"site_url": SHOP + "/", "fetch_mode": "browser", "browser": "chromium"})
    body = denied_svc.post("/v1/samples", {"config": cfg, "profile": profile(), "urls": [f"{SHOP}/jsredirect"]}).json()
    assert not any("intern" in (s.get("distilled_html") or "") for s in body["samples"])
    assert any(w["code"] == "private_address_refused" for w in body["warnings"]), body
    assert not any(k.endswith("/private/secret") or k.endswith("/pixel.png") for k in fixture_world.state()["hits"])


def test_s4_nested_sitemap_on_internal_host_is_not_fetched(denied_svc, fixture_world):
    fixture_world.set(8601, reset_hits=True)
    cfg = merged_config(denied_svc, {"site_url": SHOP + "/", "sitemap_url": SHOP + "/sitemap_evil.xml", "fetch_mode": "http"})
    body = denied_svc.post("/v1/analyze", {"config": cfg, "profile": profile()}).json()
    assert body["sitemap"]["url_count"] == 8, body["sitemap"]
    assert "8601/sitemap-internal.xml" not in fixture_world.state()["hits"]


def test_webhook_on_a_private_http_address_is_delivered_while_crawling_it_is_refused(denied_svc, fixture_world):
    body = denied_svc.post("/v1/config/validate", {"run_inputs": {"site_url": f"{HOOK}/hook/x"}, "profile": profile()}).json()
    assert body["valid"] is False and body["errors"][0]["code"] == "private_address_refused"
    ensure_mappings(denied_svc, SHOP_SITE, PRODUCT_MAPPING)
    fixture_world.set(8604, hook_on=True, reset_deliveries=True)
    cfg = merged_config(denied_svc, {"discovery_mode": "url_list", "urls": [{"url": f"{SHOP}/produkt/leuchte-07"}], "fetch_mode": "http", **fast()})
    run = denied_svc.wait_run(start(denied_svc, cfg, "hook-private").json()["run_id"])
    eid = next(e for e in run["events"] if e["event_type"] == "run_completed")["event_id"]
    got = _wait(lambda: [d for d in fixture_world.deliveries() if json.loads(d["body"]).get("event_id") == eid and d["accepted"]], what="delivery")
    assert got[0]["signature_ok"] and got[0]["body_signature_ok"]
    assert set(got[0]["headers"]) >= {"x-scraper-signature", "x-scraper-timestamp", "x-scraper-body-signature", "idempotency-key"}


# ---------------------------------------------------------------- delivery: parked events revive after a restart with the right key
def test_parked_events_revive_after_restart_with_the_right_key(rsvc, fixture_world):
    ensure_mappings(rsvc, SHOP_SITE, PRODUCT_MAPPING)
    fixture_world.set(8604, hook_on=False, reset_deliveries=True)
    cfg = merged_config(rsvc, {"discovery_mode": "url_list", "urls": [{"url": f"{SHOP}/produkt/leuchte-09"}], "fetch_mode": "http", **fast()})
    run = rsvc.wait_run(start(rsvc, cfg, "revive-parked").json()["run_id"])
    eid = next(e for e in run["events"] if e["event_type"] == "run_completed")["event_id"]
    rsvc.kill()
    rsvc.start(WSS_SECRETS_KEY="another-secrets-key-0000000000")
    _wait(lambda: next(e for e in rsvc.get(f"/v1/runs/{run['run_id']}").json()["events"] if e["event_id"] == eid)["delivery"]["last_error"] == "signing_secret_unavailable",
          what="parked")
    fixture_world.set(8604, hook_on=True)
    rsvc.kill()
    rsvc.start(WSS_SECRETS_KEY=SECRETS_KEY)
    _wait(lambda: [d for d in fixture_world.deliveries() if json.loads(d["body"]).get("event_id") == eid and d["accepted"]], what="revived delivery")


# ---------------------------------------------------------------- url_include filters extraction, not crawling
def test_crawl_with_include_follows_listings_and_extracts_only_matches(service, fixture_world):
    ensure_mappings(service, "plain.test:8602", PRODUCT_MAPPING)
    cfg = merged_config(service, {"site_url": "http://plain.test:8602/", "discovery_mode": "crawl", "fetch_mode": "http",
                                  "url_include": ["/produkt/"], **fast(max_concurrency=2)})
    run = service.wait_run(start(service, cfg, "crawl-include").json()["run_id"])
    lines = [json.loads(line) for line in service.get(run["result"]["download_path"]).text.splitlines()]
    # products are linked only from category pages, which the include pattern does not match
    assert len(lines) >= 12 and all("/produkt/" in r["source_url"] for r in lines), (run["counters"], len(lines))
    assert run["counters"]["pages_by_class"].get("not_target", 0) >= 3  # home + categories (+ parts) crawled for links


# ---------------------------------------------------------------- samples without structured data and without hints
def test_samples_guess_target_pages_without_hints(service, fixture_world):
    cfg = merged_config(service, {"site_url": "http://nold.test:8602/", "fetch_mode": "http", "sample_count": 3})
    body = check(service.post("/v1/samples", {"config": cfg, "profile": profile()}), "samples.schema.json", 200)
    assert len(body["samples"]) == 3, body["warnings"]
    assert all("/produkt/" in s["url"] for s in body["samples"])
    guessed = [w for w in body["warnings"] if w["code"] == "target_pages_guessed"]
    assert guessed and "/produkt/*" in guessed[0]["message"]


def test_samples_on_a_site_of_listings_only_find_nothing(service, fixture_world):
    cfg = merged_config(service, {"site_url": "http://listings.test:8602/", "fetch_mode": "http", "sample_count": 3})
    body = service.post("/v1/samples", {"config": cfg, "profile": profile()}).json()
    assert body["samples"] == [] and not [w for w in body["warnings"] if w["code"] == "target_pages_guessed"]


# ---------------------------------------------------------------- target-looking links first
def test_crawl_trial_fetches_products_before_every_listing(service, fixture_world):
    ensure_mappings(service, "plain.test:8602", PRODUCT_MAPPING)
    fixture_world.set(8602, reset_hits=True)
    cfg = merged_config(service, {"site_url": "http://plain.test:8602/", "discovery_mode": "crawl", "fetch_mode": "http", "max_records": 3,
                                  **fast(max_concurrency=1)})
    run = service.wait_run(start(service, cfg, "crawl-targets-first").json()["run_id"])
    hits = fixture_world.state(8602)["hits"]
    print("TRIAL pages_fetched", run["counters"]["pages_fetched"], sorted(k for k in hits if "robots" not in k))
    assert run["counters"]["records_extracted"] == 3
    assert hits.get("8602/kategorie/teile", 0) == 0  # the second listing is never needed
    assert run["counters"]["pages_fetched"] <= 5
