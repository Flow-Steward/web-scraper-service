"""Browsers through our plugins, adaptive predictor, proxy tiers with escalation on a block only, delays."""

from __future__ import annotations

import json

import pytest

from tests.conftest import PART_MAPPING, PRODUCT_MAPPING, SHOP, SHOP_SITE, ensure_mappings, fast, merged_config, profile, start
from app.core.timeutil import parse

TIERS = [
    {"tier": 1, "provider": "generic", "protocol": "http", "host": "127.0.0.1", "port": 8606, "auth_mode": "user_password",
     "username": "tier1user", "password": "tier1pass", "session_mode": "sticky", "sticky_minutes": 10},
    {"tier": 2, "provider": "generic", "protocol": "http", "host": "127.0.0.1", "port": 8607, "auth_mode": "user_password",
     "username": "tier2user", "password": "tier2pass", "session_mode": "sticky", "sticky_minutes": 10},
]


@pytest.mark.parametrize("browser", ["camoufox", "chromium"])
def test_adaptive_js_pages_use_the_browser(service, fixture_world, browser):
    ensure_mappings(service, SHOP_SITE, PRODUCT_MAPPING, PART_MAPPING)
    urls = [{"url": f"{SHOP}/produkt/leuchte-1{i}"} for i in (1, 2)] + [{"url": f"{SHOP}/js/js-leuchte-0{i}"} for i in (1, 2, 3)]
    cfg = merged_config(service, {"discovery_mode": "url_list", "urls": urls, "browser": browser, **fast(max_concurrency=1)})
    run = service.wait_run(start(service, cfg, f"fetch-js-{browser}").json()["run_id"])
    c = run["counters"]
    assert run["state"] == "completed" and c["records_extracted"] == 5, (c, run["errors"])
    assert c["browser_fetches"] == 3 and c["http_fetches"] == 2
    assert run["resolved"]["browser"] == browser
    recs = [json.loads(l) for l in service.get(run["result"]["download_path"]).text.splitlines()]
    js = next(r for r in recs if "/js/" in r["source_url"])
    assert js["data"]["title"].startswith("JS Leuchte") and js["data"]["price"] > 300


def test_browser_fetch_mode_and_blocked_resources(service, fixture_world):
    ensure_mappings(service, SHOP_SITE, PRODUCT_MAPPING, PART_MAPPING)
    fixture_world.set(8601, reset_hits=True)
    cfg = merged_config(service, {"discovery_mode": "url_list", "urls": [{"url": f"{SHOP}/produkt/leuchte-14"}], "fetch_mode": "browser",
                                  "browser": "chromium", "block_heavy_resources": True, **fast()})
    run = service.wait_run(start(service, cfg, "fetch-browser-mode").json()["run_id"])
    assert run["counters"]["browser_fetches"] == 1 and run["counters"]["records_extracted"] == 1
    hits = fixture_world.state()["hits"]
    assert not any("/media/" in k for k in hits)  # images are not loaded


def test_proxy_tiers_escalate_only_on_a_block(service, fixture_world):
    ensure_mappings(service, SHOP_SITE, PRODUCT_MAPPING, PART_MAPPING)
    prof = profile(proxy_tiers=TIERS)
    # ordinary pages and a 404 through tier 1: no escalation
    cfg = merged_config(service, {"discovery_mode": "url_list", "urls": [{"url": f"{SHOP}/produkt/leuchte-15"}, {"url": f"{SHOP}/gone/x"}],
                                  "fetch_mode": "http", **fast(max_concurrency=1)}, prof=prof)
    run = service.wait_run(start(service, cfg, "fetch-tier-plain", prof=prof).json()["run_id"])
    # two pages and robots.txt, all through tier 1 (robots.txt and sitemaps use the job's tiers)
    assert run["counters"]["proxy_requests_by_tier"] == {"1": 3} and run["counters"]["current_proxy_tier"] == 1
    assert run["profile"]["proxy_tiers"][0] == {k: v for k, v in TIERS[0].items() if k not in ("username", "password")}
    # tier 1 is blocked on /tiered/: the next tier is used after the block and kept for the host
    cfg = merged_config(service, {"discovery_mode": "url_list", "urls": [{"url": f"{SHOP}/tiered/{i}"} for i in range(1, 5)],
                                  "fetch_mode": "http", **fast(max_concurrency=1)}, prof=prof)
    run = service.wait_run(start(service, cfg, "fetch-tier-block", prof=prof).json()["run_id"])
    c = run["counters"]
    assert c["records_extracted"] == 4, (c, run["errors"])
    assert c["proxy_requests_by_tier"]["1"] == 2 and c["proxy_requests_by_tier"]["2"] >= 4 and c["current_proxy_tier"] == 2
    # a hard block is counted in hard_blocks and tier_escalations, not as a challenge
    p = run["protection"]
    assert p["hard_blocks"] == 1 and p["tier_escalations"] == 1 and p["passed_by_step"] == {} and p["challenges_met"] == 0
    text = json.dumps(run)
    assert "tier1pass" not in text and "tier2pass" not in text


def test_random_delay_between_requests(service, fixture_world):
    ensure_mappings(service, SHOP_SITE, PRODUCT_MAPPING, PART_MAPPING)
    urls = [{"url": f"{SHOP}/produkt/leuchte-2{i}"} for i in range(4)]
    cfg = merged_config(service, {"discovery_mode": "url_list", "urls": urls, "fetch_mode": "http", "delay_min_seconds": 1, "delay_max_seconds": 1.2, "max_concurrency": 4})
    run = service.wait_run(start(service, cfg, "fetch-delay").json()["run_id"])
    took = (parse(run["finished_at"]) - parse(run["started_at"])).total_seconds()
    assert run["counters"]["records_extracted"] == 4 and took >= 3.0  # 4 requests to one host, >= 1 s apart
