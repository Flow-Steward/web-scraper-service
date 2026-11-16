"""Page classes, stored pages, repair and mapping requests, versions, completion gate."""

from __future__ import annotations

import copy
import json
import time

from tests.conftest import PRODUCT_MAPPING, check, ensure_mappings, err, fast, merged_config, profile, start

REPAIRED = copy.deepcopy(PRODUCT_MAPPING)
for f in REPAIRED["fields"]:
    if f["name"] == "title":
        f["sources"].append({"kind": "css", "selector": "h1.pdp-name", "extract": "text"})
    if f["name"] == "price":
        f["sources"].append({"kind": "css", "selector": "div.pdp-price", "extract": "text", "transforms": [{"op": "price", "decimal_separator": "comma"}]})


def _wait_for(svc, run_id, pred, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = svc.get(f"/v1/runs/{run_id}").json()
        if pred(run):
            return run
        time.sleep(0.3)
    raise AssertionError(json.dumps(run)[:4000] + svc.log()[-3000:])


def _start_layout_run(service, fixture_world, host, key, n=12):
    site = f"{host}:8601"
    ensure_mappings(service, site, PRODUCT_MAPPING)
    fixture_world.set(8601, reset_hits=True, layout=1, layout_switch_after=4)
    urls = [{"url": f"http://{host}:8601/produkt/leuchte-{i:02d}"} for i in range(1, n + 1)]
    cfg = merged_config(service, {"discovery_mode": "url_list", "urls": urls, "fetch_mode": "http", "required_fields": ["title", "price"], **fast(max_concurrency=1)})
    return site, start(service, cfg, key).json()["run_id"]


def test_layout_change_triggers_repair_and_stored_pages_are_reextracted(service, fixture_world):
    site, run_id = _start_layout_run(service, fixture_world, "repair.test", "repair-ok")
    try:
        run = _wait_for(service, run_id, lambda r: r["progress"]["open_mapping_requests"] == 1 and r["progress"]["queue_pending"] == 0 and r["progress"]["in_progress"] == 0)
        # the job waits: no run_completed while a mapping request is open
        time.sleep(2)
        run = check(service.get(f"/v1/runs/{run_id}"), "run_summary.schema.json", 200)
        assert run["state"] == "running" and not [e for e in run["events"] if e["event_type"] == "run_completed"]
        tpl = next(t for t in run["templates"] if t["template_id"] == "product")
        assert tpl["status"] == "repairing" and tpl["stored_pages_pending"] >= 1
        event = next(e for e in run["events"] if e["event_type"] == "mapping_required")
        mrq_id = event["resource_id"]
        mrq = check(service.get(f"/v1/mapping-requests/{mrq_id}"), "mapping_request.schema.json", 200)
        assert mrq["reason"] == "incomplete" and mrq["template_id"] == "product" and mrq["status"] == "open"
        assert mrq["expected_version"] == mrq["previous"]["version"] and mrq["samples"] and mrq["failures"]["pages_failing"] >= 1
        assert mrq["instructions"]["required_fields"] == ["title", "price"]
        # the candidate is tested on the request's samples before activation
        sample_ids = [s["sample_id"] for s in mrq["samples"]]
        t = service.post("/v1/mappings/test", {"site": site, "mappings": [REPAIRED], "sample_ids": sample_ids, "required_fill_threshold": 0.9, "required_fields": ["title", "price"]}).json()
        assert t["passed"], t
        act = check(service.put(f"/v1/sites/{site}/mappings", {"template_id": "product", "expected_version": mrq["expected_version"], "mapping": REPAIRED,
                                                                 "origin": {"run_id": run_id, "mapping_request_id": mrq_id}}),
                    "mapping.schema.json#/$defs/activate_response", 200)
        assert act["changed"] and act["resolved_mapping_request_ids"] == [mrq_id]
        # a second activation built against the same version is refused
        err(service.put(f"/v1/sites/{site}/mappings", {"template_id": "product", "expected_version": mrq["expected_version"], "mapping": PRODUCT_MAPPING}), 409, "mapping_version_conflict")
        run = service.wait_run(run_id)
        assert run["state"] == "completed" and run["counters"]["records_extracted"] == 12, run["counters"]
        tpl = next(t for t in run["templates"] if t["template_id"] == "product")
        assert tpl["status"] == "repaired" and len(tpl["mapping_versions_used"]) == 2 and tpl["stored_pages_pending"] == 0
        assert run["counters"]["repairs_started"] == 1 and run["counters"]["repairs_succeeded"] == 1
        assert run["outcome"] == "complete" and run["mapping_requests"][0]["status"] == "resolved"
        closed = service.get(f"/v1/mapping-requests/{mrq_id}").json()
        assert closed["resolution"]["status"] == "resolved" and closed["resolution"]["version"] == act["active"]["version"]
        err(service.post(f"/v1/mapping-requests/{mrq_id}/fail", {"failure_reason": "late"}), 409, "mapping_request_closed")
        lines = [json.loads(l) for l in service.get(run["result"]["download_path"]).text.splitlines()]
        assert {l["mapping_version"] for l in lines} == {act["active"]["version"] - 1, act["active"]["version"]}
    finally:
        fixture_world.set(8601, layout=1, layout_switch_after=None)


def test_template_that_cannot_be_repaired_ends_partial(service, fixture_world):
    site, run_id = _start_layout_run(service, fixture_world, "fail.test", "repair-fail", n=10)
    try:
        run = _wait_for(service, run_id, lambda r: r["progress"]["open_mapping_requests"] == 1)
        mrq_id = next(e for e in run["events"] if e["event_type"] == "mapping_required")["resource_id"]
        failed = check(service.post(f"/v1/mapping-requests/{mrq_id}/fail", {"failure_reason": "build_mapping did not pass", "attempts": 3}), "mapping_request.schema.json", 200)
        assert failed["status"] == "failed"
        again = service.post(f"/v1/mapping-requests/{mrq_id}/fail", {"failure_reason": "again"})
        assert again.status_code == 200 and again.json()["resolution"]["failure_reason"] == "build_mapping did not pass"
        run = service.wait_run(run_id)
        assert run["state"] == "completed" and run["outcome"] == "partial"
        tpl = next(t for t in run["templates"] if t["template_id"] == "product")
        assert tpl["status"] == "failed"
        problems = [e for e in run["events"] if e["event_type"] == "problem"]
        assert len(problems) == 1
        unex = service.get(f"/v1/runs/{run_id}/unextracted", params={"limit": 200}).json()
        # every fetched target page is either a record or in the unextracted list
        assert run["counters"]["records_extracted"] + unex["total"] == 10
        assert {i["reason"] for i in unex["items"]} == {"template_failed"}
    finally:
        fixture_world.set(8601, layout=1, layout_switch_after=None)


def test_new_template_through_no_template_request(service, fixture_world):
    site = "parts.test:8602"
    ensure_mappings(service, site, PRODUCT_MAPPING)
    urls = [{"url": f"http://parts.test:8602/ersatzteile/teil-0{i}"} for i in range(1, 5)] + [{"url": "http://parts.test:8602/produkt/leuchte-01"}]
    cfg = merged_config(service, {"discovery_mode": "url_list", "urls": urls, "fetch_mode": "http", **fast(max_concurrency=1)})
    run_id = start(service, cfg, "repair-new-template").json()["run_id"]
    run = _wait_for(service, run_id, lambda r: r["progress"]["open_mapping_requests"] == 1 and r["progress"]["queue_pending"] == 0 and r["progress"]["in_progress"] == 0)
    mrq_id = next(e for e in run["events"] if e["event_type"] == "mapping_required")["resource_id"]
    mrq = service.get(f"/v1/mapping-requests/{mrq_id}").json()
    assert mrq["reason"] == "no_template" and mrq["template_id"] is None and mrq["expected_version"] == 0 and mrq["previous"] is None
    assert mrq["suggested_template_id"] == "ersatzteile"
    new = {
        "schema_version": 1, "template_id": mrq["suggested_template_id"], "match": {"url_patterns": ["/ersatzteile/"], "selector": "div.part-sheet"},
        "fields": [
            {"name": "title", "type": "text", "required": True, "sources": [{"kind": "css", "selector": "h2.part-name", "extract": "text"}]},
            {"name": "price", "type": "price", "sources": [{"kind": "css", "selector": ".part-price", "extract": "text"}]},
            {"name": "currency", "type": "currency", "sources": [{"kind": "css", "selector": ".part-price", "extract": "text"}]},
        ],
    }
    act = service.put(f"/v1/sites/{site}/mappings", {"template_id": new["template_id"], "expected_version": 0, "mapping": new}).json()
    assert act["active"]["version"] == 1 and act["resolved_mapping_request_ids"] == [mrq_id]
    run = service.wait_run(run_id)
    assert run["counters"]["records_extracted"] == 5 and run["outcome"] == "complete", run["counters"]
    lines = [json.loads(l) for l in service.get(run["result"]["download_path"]).text.splitlines()]
    part = next(l for l in lines if l["template_id"] == "ersatzteile")
    assert part["data"]["currency"] == "EUR" and isinstance(part["data"]["price"], float)


def test_operator_stop_cancels_requests_and_lists_stored_pages(service, fixture_world):
    site, run_id = _start_layout_run(service, fixture_world, "repair.test", "repair-stop", n=8)
    try:
        run = _wait_for(service, run_id, lambda r: r["progress"]["open_mapping_requests"] == 1)
        stopped = service.post(f"/v1/runs/{run_id}/stop", {"note": "enough"}).json()
        assert stopped["state"] in ("stopping", "completed")
        run = service.wait_run(run_id)
        assert run["stop_reason"] == "stopped" and run["outcome"] == "partial"
        assert run["mapping_requests"][0]["status"] == "cancelled"
        reasons = {i["reason"] for i in service.get(f"/v1/runs/{run_id}/unextracted", params={"limit": 200}).json()["items"]}
        assert "stopped" in reasons
    finally:
        fixture_world.set(8601, layout=1, layout_switch_after=None)
