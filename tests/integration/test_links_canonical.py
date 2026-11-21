"""On links.test: link rules, canonical duplicates, the same-site
queue and the stall watchdog, each on a service of its own."""

from __future__ import annotations

import json
import time

import pytest

from tests.conftest import check, ensure_mappings, fast, merged_config, new_service, start

L = "http://links.test:8602"
SITE = "links.test:8602"
ITEM = {"schema_version": 1, "template_id": "item", "match": {"url_patterns": ["/lk/(cat/a/)?p\\d+\\.html$", "/lk/item\\?id=\\d+$"]}, "fields": [
    {"name": "title", "type": "text", "required": True, "sources": [{"kind": "jsonld", "schema_type": "Product", "path": "name"}]},
    {"name": "sku", "type": "text", "sources": [{"kind": "jsonld", "schema_type": "Product", "path": "sku"}]}]}


@pytest.fixture(scope="module")
def links_service(fixture_world):
    svc = new_service(8101, WSS_LIMIT_MAX_CONCURRENT_JOBS="4").start()
    ensure_mappings(svc, SITE, ITEM)
    yield svc
    svc.terminate()
    fixture_world.set(8602, lk_wrong=False)


def _crawl(svc, key: str, **extra) -> dict:
    cfg = merged_config(svc, {"site_url": L + "/lk/", "discovery_mode": "crawl", "fetch_mode": "http", "max_retries": 0, "trap_pattern_budget": 5,
                              **fast(max_concurrency=1), **extra})
    r = start(svc, cfg, key)
    assert r.status_code == 201, r.text
    return r.json()


def _records(svc, run: dict) -> list[dict]:
    return [json.loads(x) for x in svc.get(run["result"]["download_path"]).text.splitlines()]


def test_link_rules_canonicals_and_traps(links_service, fixture_world):
    svc = links_service
    fixture_world.set(8602, reset_hits=True, lk_wrong=False)
    run = svc.wait_run(_crawl(svc, "lk-1")["run_id"])
    run = check(svc.get(f"/v1/runs/{run['run_id']}"), "run_summary.schema.json", 200)
    assert run["state"] == "completed", run
    recs = _records(svc, run)
    by_url = {record["source_url"]: record for record in recs}
    expected_canonicals = {f"{L}/lk/p{number}.html" for number in (1, 2, 3, 4, 5, 9)}
    assert len(recs) == 7
    assert set(by_url) == expected_canonicals | {f"{L}/lk/item?id=2"}
    assert len({record["record_id"] for record in recs}) == 7
    for number in (1, 2, 3, 4, 5, 9):
        assert by_url[f"{L}/lk/p{number}.html"]["data"]["sku"] == f"LK-{number}"
    # A query-free path alias still adopts its declared canonical.
    assert by_url[f"{L}/lk/p2.html"]["requested_url"] == f"{L}/lk/cat/a/p2.html"
    # An SEO canonical cannot erase a non-pagination query identity.
    query_item = by_url[f"{L}/lk/item?id=2"]
    assert query_item["data"]["sku"] == "LK-2"
    assert "requested_url" not in query_item
    c = run["counters"]
    skipped = c["links_skipped"]
    assert skipped["scheme"] >= 3 and skipped["messenger_social"] >= 2, skipped  # mailto, tel, whatsapp:, viber:; wa.me, sharer
    assert skipped["sensitive_path"] >= 3 and skipped["file_extension"] >= 1, skipped  # login, cart, wishlist links on products
    assert skipped["facet_params"] >= 1 and skipped["nofollow"] >= 1 and skipped["trap_budget"] >= 1, skipped
    assert c["pages_by_class"].get("duplicate", 0) >= 1 and skipped.get("canonical", 0) == c["pages_by_class"]["duplicate"], c
    hits = fixture_world.state(8602)["hits"]
    for path in ("/lk/customer/account/login/", "/lk/checkout/cart/", "/lk/manual.pdf", "/lk/nofollow-only/"):
        assert not hits.get("8602" + path), path
    assert not any("wishlist" in k for k in hits), [k for k in hits if "wishlist" in k]
    assert hits.get("8602/lk/authentic-lamps/") == 1  # "auth" never matches "authentic"
    assert hits.get("8602/lk/events", 0) <= 8, hits.get("8602/lk/events")  # the calendar stopped by the trap budget
    assert hits.get("8602/lk/p1.html") == 1  # the SID link is the same page
    codes = {e["code"] for e in run["errors"]}
    assert "trap_suspected" in codes and "canonical_ignored" not in codes
    assert any(t["kind"] == "trap" for t in run["timeline"])
    assert "duplicate address" in run["completeness"]["sentence"]
    assert run["completeness"]["not_collected"] == 0


def test_wrong_canonicals_are_ignored(links_service, fixture_world):
    svc = links_service
    fixture_world.set(8602, reset_hits=True, lk_wrong=True)
    run = svc.wait_run(_crawl(svc, "lk-wrong")["run_id"])
    run = check(svc.get(f"/v1/runs/{run['run_id']}"), "run_summary.schema.json", 200)
    recs = _records(svc, run)
    # every product is there under its own address (aliases count once each again: canonicals are ignored)
    assert {r["data"]["sku"] for r in recs} == {"LK-1", "LK-2", "LK-3", "LK-4", "LK-5", "LK-9"}, [r["data"]["sku"] for r in recs]
    assert not any(r["source_url"].endswith("/lk/cat/a/") for r in recs), [r["source_url"] for r in recs]
    assert len({r["record_id"] for r in recs}) == len(recs)
    assert any(e["code"] == "canonical_ignored" for e in run["errors"]), run["errors"]
    fixture_world.set(8602, lk_wrong=False)


def test_canonical_rule_off_and_analysis_lists_the_rules(links_service, fixture_world):
    svc = links_service
    fixture_world.set(8602, lk_wrong=False)
    run = svc.wait_run(_crawl(svc, "lk-off", link_rules_off=["canonical", "facet_params"])["run_id"])
    assert run["counters"]["pages_by_class"].get("duplicate", 0) == 0
    assert "facet_params" not in run["counters"]["links_skipped"]
    assert len(_records(svc, run)) > 6  # aliases collected as their own pages
    cfg = merged_config(svc, {"site_url": L + "/lk/", "link_rules_off": ["nofollow"]})
    from tests.conftest import profile

    a = check(svc.post("/v1/analyze", {"config": cfg, "profile": profile()}), "analysis.schema.json", 200)
    rules = {r["name"]: r["on"] for r in a["link_rules"]["rules"]}
    assert rules["nofollow"] is False and rules["sensitive_path"] is True and "wishlist" in a["link_rules"]["sensitive_segments"]


def test_second_run_of_a_site_waits(links_service, fixture_world):
    svc = links_service
    first = _crawl(svc, "lk-q1", delay_min_seconds=0.3, delay_max_seconds=0.3)
    deadline = time.time() + 30
    while svc.get(f"/v1/runs/{first['run_id']}").json()["state"] != "running" and time.time() < deadline:
        time.sleep(0.2)
    second = _crawl(svc, "lk-q2")
    assert second["state"] == "queued"
    assert second["queued_reason"]["code"] == "same_site_running" and second["queued_reason"]["run_id"] == first["run_id"]
    check(svc.get(f"/v1/runs/{second['run_id']}"), "run_summary.schema.json", 200)
    listed = check(svc.get("/v1/runs", params={"site": SITE, "limit": 50}), "run_summary.schema.json#/$defs/run_list", 200)["items"]
    row = next(i for i in listed if i["run_id"] == second["run_id"])
    assert row["queued_reason"]["code"] == "same_site_running" and row["completeness"]["target_pages_found"] == 0
    a = svc.wait_run(first["run_id"])
    b = svc.wait_run(second["run_id"])
    assert b["state"] == "completed" and b["started_at"] >= a["finished_at"], (a["finished_at"], b["started_at"])
    assert "queued_reason" not in b
    listed = check(svc.get("/v1/runs", params={"site": SITE, "limit": 50}), "run_summary.schema.json#/$defs/run_list", 200)["items"]
    row = next(i for i in listed if i["run_id"] == second["run_id"])
    full = svc.get(f"/v1/runs/{second['run_id']}").json()["completeness"]
    assert row["completeness"] == {k: full[k] for k in ("target_pages_found", "pages_collected", "records", "not_collected", "not_collected_by_reason")}
    assert row["stop_reason"] == "finished" and row["records_extracted"] == full["records"]


def test_stalled_run_is_reported_then_stopped(fixture_world):
    svc = new_service(8102, WSS_STALL_SECONDS="3").start()
    try:
        ensure_mappings(svc, SITE, ITEM)
        cfg = merged_config(svc, {"discovery_mode": "url_list", "urls": [{"url": L + "/lk/hang/1"}], "fetch_mode": "http", "max_retries": 0, **fast()})
        run = start(svc, cfg, "lk-hang").json()
        done = svc.wait_run(run["run_id"], timeout=120)
        done = check(svc.get(f"/v1/runs/{done['run_id']}"), "run_summary.schema.json", 200)
        assert done["state"] == "completed" and done["stop_reason"] == "stalled", done["stop_reason"]
        kinds = [t["kind"] for t in done["timeline"]]
        assert kinds.count("stalled") >= 2, done["timeline"]
        assert any("hang/1" in t["message"] for t in done["timeline"] if t["kind"] == "stalled")
        assert "no progress" in done["explanation"]["sentence"] or "stalled" in done["explanation"]["sentence"]
        un = svc.get(f"/v1/runs/{run['run_id']}/unextracted").json()["items"]
        assert [u["reason"] for u in un] == ["stopped"]
        problems = [e for e in done["events"] if e["event_type"] == "problem"]
        assert problems and svc.get("/health").json()["timeouts"]["stall_seconds"] == 3
    finally:
        svc.terminate()
