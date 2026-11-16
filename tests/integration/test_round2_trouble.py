"""Stop on trouble, completeness / run-to-run comparison."""

from __future__ import annotations

import pytest

from tests.conftest import PRODUCT_MAPPING, SHOP, check, ensure_mappings, fast, merged_config, new_service, start


@pytest.fixture(scope="module")
def trouble_service(fixture_world):
    svc = new_service(8095, WSS_BREAKER_FAILURES="3", WSS_BREAKER_PAUSE_SECONDS="0.5", WSS_BREAKER_MAX_PAUSES="1",
                      WSS_SITEMAP_OUTDATED_MIN="5", WSS_SITEMAP_OUTDATED_SHARE="0.3").start()
    yield svc
    svc.terminate()


def _run(svc, key: str, inputs: dict) -> dict:
    cfg = merged_config(svc, inputs)
    run_id = start(svc, cfg, key).json()["run_id"]
    svc.wait_run(run_id)
    return check(svc.get(f"/v1/runs/{run_id}"), "run_summary.schema.json", 200)


def test_a_site_that_is_down_stops_the_run_with_the_reason(trouble_service, fixture_world):
    urls = [{"url": f"{SHOP}/down/item-{i}"} for i in range(15)]
    run = _run(trouble_service, "down-site", {"discovery_mode": "url_list", "urls": urls, "fetch_mode": "http", "max_retries": 0,
                                               **fast(max_concurrency=1)})
    assert run["stop_reason"] == "site_unavailable" and run["outcome"] == "partial", (run["stop_reason"], run["counters"])
    assert run["counters"]["pages_fetched"] < 15  # it stopped loading a site that is down
    kinds = [t["kind"] for t in run["timeline"]]
    assert kinds[0] == "started" and "paused" in kinds and "stopped" in kinds and kinds[-1] == "finished", run["timeline"]
    problems = [e for e in run["events"] if e["event_type"] == "problem"]
    assert problems, run["events"]
    assert run["explanation"]["sentence"].startswith("Stopped: the site was unavailable") and "try again later" in run["explanation"]["advice"].lower()
    assert "shop.test looks unavailable" in run["explanation"]["sentence"]


def test_an_outdated_sitemap_is_reported(trouble_service, fixture_world):
    ensure_mappings(trouble_service, "oldmap.test:8602", PRODUCT_MAPPING)
    run = _run(trouble_service, "old-sitemap", {"site_url": "http://oldmap.test:8602/", "discovery_mode": "sitemap", "fetch_mode": "http",
                                                 **fast(max_concurrency=1)})
    assert any(t["kind"] == "problem" and "sitemap looks outdated" in t["message"] for t in run["timeline"]), run["timeline"]
    assert "Discovery = sitemap and crawl" in next(t["message"] for t in run["timeline"] if "sitemap looks outdated" in t["message"])
    comp = run["completeness"]
    assert comp["pages_collected"] == 2 and comp["not_collected_by_reason"].get("not_found") == 8 and comp["target_pages_found"] == 10, comp
    assert comp["sentence"] == "found 10 target pages, collected 2, 8 not collected: 8 not found (404/410)"


def test_completeness_and_comparison_with_the_previous_run(trouble_service, fixture_world):
    plain = "http://plain.test:8602"  # a site no other run of this service touched
    ensure_mappings(trouble_service, "plain.test:8602", PRODUCT_MAPPING)
    three = [{"url": f"{plain}/produkt/leuchte-0{i}"} for i in (1, 2, 3)]
    first = _run(trouble_service, "cmp-1", {"discovery_mode": "url_list", "urls": three, "fetch_mode": "http", **fast(max_concurrency=1)})
    assert first["completeness"]["sentence"] == "found 3 target pages, collected 3, none missing" and "comparison" not in first
    assert first["explanation"]["sentence"] == "Finished; found 3 target pages, collected 3, none missing."
    # the same site again, two of the pages now gone: found but not collected
    missing = [three[0], {"url": f"{plain}/gone/leuchte-02"}, {"url": f"{plain}/gone/leuchte-03"}]
    second = _run(trouble_service, "cmp-2", {"discovery_mode": "url_list", "urls": missing, "fetch_mode": "http", **fast(max_concurrency=1)})
    cmp = second["comparison"]
    assert cmp["run_id"] == first["run_id"] and cmp["change"]["records"] == -2 and cmp["change"]["target_pages_found"] == 0, cmp
    assert "records fell from 3 to 1" in cmp["warning"] and "found but not collected" in cmp["warning"], cmp["warning"]
    assert "records fell from 3 to 1" in second["explanation"]["sentence"]
    # a smaller site: fewer target pages found
    third = _run(trouble_service, "cmp-3", {"discovery_mode": "url_list", "urls": three[:1], "fetch_mode": "http", **fast(max_concurrency=1)})
    assert third["comparison"]["run_id"] == second["run_id"] and "warning" not in third["comparison"]  # 1 record, as before
    trial = _run(trouble_service, "cmp-trial", {"discovery_mode": "url_list", "urls": three, "fetch_mode": "http", "max_records": 1,
                                                **fast(max_concurrency=1)})
    assert "comparison" not in trial  # a max_records trial is not compared
