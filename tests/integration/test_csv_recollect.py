"""CSV beside JSON Lines (README 7.5) and re-collecting a finished run's missed pages
(README 4.10), on week.test with a service of its own."""

from __future__ import annotations

import csv
import io
import json

import pytest

from tests.conftest import check, ensure_mappings, err, fast, merged_config, new_service, platform, profile, start

W = "http://week.test:8602"
SITE = "week.test:8602"
ITEM = {"schema_version": 1, "template_id": "item", "match": {"url_patterns": ["/wk/p\\d+$"]}, "fields": [
    {"name": "title", "type": "text", "required": True, "sources": [{"kind": "jsonld", "schema_type": "Product", "path": "name"}]},
    {"name": "sku", "type": "text", "sources": [{"kind": "jsonld", "schema_type": "Product", "path": "sku"}]},
    {"name": "price", "type": "price", "sources": [{"kind": "jsonld", "schema_type": "Product", "path": "offers.price", "transforms": [{"op": "price", "decimal_separator": "dot"}]}]},
    {"name": "in_stock", "type": "boolean", "sources": [{"kind": "jsonld", "schema_type": "Product", "path": "offers.availability"}]},
    {"name": "images", "type": "image_url", "multiple": True, "sources": [{"kind": "jsonld", "schema_type": "Product", "path": "image.*", "transforms": [{"op": "absolute_url"}]}]},
    {"name": "note", "type": "text", "sources": [{"kind": "css", "selector": "div.product p", "extract": "text"}]}]}


@pytest.fixture(scope="module")
def week_service(fixture_world):
    svc = new_service(8100).start()
    ensure_mappings(svc, SITE, ITEM)
    yield svc
    svc.terminate()
    fixture_world.set(8602, week=1, wk_fail=[])


def _crawl(svc, key: str, **extra) -> dict:
    cfg = merged_config(svc, {"site_url": W + "/wk/", "discovery_mode": "crawl", "fetch_mode": "http", "max_retries": 0, **fast(), **extra})
    r = start(svc, cfg, key)
    assert r.status_code == 201, r.text
    return svc.wait_run(r.json()["run_id"])


def _csv(text: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(text), delimiter=";"))


def test_result_csv_beside_json_lines(week_service, fixture_world):
    svc = week_service
    fixture_world.set(8602, week=1, wk_fail=[])
    run = _crawl(svc, "csv-1")
    assert run["state"] == "completed" and run["counters"]["records_extracted"] == 5, run["counters"]
    res = check(svc.get(f"/v1/runs/{run['run_id']}/result"), "result_descriptor.schema.json#/$defs/service_result", 200)
    assert res["csv"]["file_name"].endswith(".csv") and res["csv"]["download_path"].endswith("?format=csv")
    assert res["fetched_at"] is None
    dl = svc.get(res["csv"]["download_path"])
    assert dl.status_code == 200 and dl.headers["content-type"].startswith("text/csv")
    assert dl.headers["etag"] == f'"{res["csv"]["sha256"]}"' and len(dl.content) == res["csv"]["size_bytes"]
    assert dl.content.startswith(b"\xef\xbb\xbf")
    text = dl.content.decode("utf-8-sig")
    assert "\r\n" in text
    rows = _csv(text)
    assert len(rows) == 5
    head = list(rows[0])
    assert head[:9] == ["record_id", "source_url", "requested_url", "source_key", "site", "template_id", "mapping_version", "item_index", "extracted_at"]
    assert head[9:14] == ["price_status", "price_max", "regular_price", "on_sale", "currency_from_default"]  # a price field
    assert head[14:] == ["data.title", "data.sku", "data.price", "data.in_stock", "data.images", "data.note"]
    lines = [json.loads(x) for x in svc.get(res["download_path"]).text.splitlines()]
    assert [r["record_id"] for r in rows] == [r["record_id"] for r in lines]  # same records, same order
    one = next(r for r in rows if r["data.sku"] == "WK-001")
    assert one["data.price"] == "11.0" and one["data.in_stock"] == "true" and one["price_status"] == "shown" and one["price_max"] == ""
    assert one["data.images"] == f"{W}/media/wk1-a.jpg | {W}/media/wk1-b.jpg"
    assert one["data.note"] == "'=SUM(A1)"  # never a formula in a spreadsheet
    after = check(svc.get(f"/v1/runs/{run['run_id']}/result"), "result_descriptor.schema.json#/$defs/service_result", 200)
    assert after["fetched_at"] is not None  # the whole CSV went out: the result counts as fetched
    assert svc.get(res["download_path"], params={"format": "xml"}).status_code == 422


def test_recollect_the_missed_pages(week_service, fixture_world):
    svc = week_service
    fixture_world.set(8602, week=2, wk_fail=[5])
    parent = _crawl(svc, "rc-parent")
    pid = parent["run_id"]
    assert parent["counters"]["records_extracted"] == 4, parent["counters"]  # p1 p3 p4 p6; p2 404, p5 500
    reasons = {u["url"].rsplit("/", 1)[1]: u["reason"] for u in svc.get(f"/v1/runs/{pid}/unextracted").json()["items"]}
    assert reasons == {"p2": "not_found", "p5": "fetch_failed"}, reasons

    fixture_world.set(8602, wk_fail=[])
    body = {"profile": profile(), "platform": platform("rc-child")}
    r = svc.client.post(f"/v1/runs/{pid}/recollect", json=body, headers={"Idempotency-Key": "rc-child-1"})
    child = check(r, "run_summary.schema.json", 201)
    assert r.headers["location"] == f"/v1/runs/{child['run_id']}"
    assert child["recollect_of"] == {"run_id": pid, "reasons": ["fetch_failed", "blocked", "incomplete", "no_template", "template_failed", "stopped"],
                                     "url_count": 1, "by_reason": {"fetch_failed": 1}}
    assert child["config"]["discovery_mode"] == "url_list" and [u["url"] for u in child["config"]["urls"]] == [W + "/wk/p5"]
    again = svc.client.post(f"/v1/runs/{pid}/recollect", json=body, headers={"Idempotency-Key": "rc-child-1"})
    assert again.status_code == 200 and again.json()["run_id"] == child["run_id"]  # idempotent
    done = check(svc.get(f"/v1/runs/{svc.wait_run(child['run_id'])['run_id']}"), "run_summary.schema.json", 200)
    assert done["state"] == "completed" and done["counters"]["records_extracted"] == 1
    assert "comparison" not in done  # a re-collect run is not compared by counts
    lines = [json.loads(x) for x in svc.get(done["result"]["download_path"]).text.splitlines()]
    assert [x["data"]["sku"] for x in lines] == ["WK-005"]  # its own result
    p = check(svc.get(f"/v1/runs/{pid}"), "run_summary.schema.json", 200)
    assert [c["run_id"] for c in p["recollected_by"]] == [child["run_id"]]

    # the 404 only when asked; it stays not found
    r = svc.client.post(f"/v1/runs/{pid}/recollect", json=dict(body, reasons=["not_found"]), headers={"Idempotency-Key": "rc-child-2"})
    c2 = check(r, "run_summary.schema.json", 201)
    assert c2["recollect_of"]["by_reason"] == {"not_found": 1}
    c2 = svc.wait_run(c2["run_id"])
    assert c2["state"] == "completed" and c2["counters"]["records_extracted"] == 0

    # the next ordinary run is compared with the parent, not with a re-collect run
    nxt = _crawl(svc, "next")
    assert nxt["comparison"]["run_id"] == pid


def test_recollect_refusals(week_service, fixture_world):
    svc = week_service
    fixture_world.set(8602, week=1, wk_fail=[])
    full = _crawl(svc, "rc-full")
    body = {"profile": profile(), "platform": platform("rc-none")}
    e = err(svc.client.post(f"/v1/runs/{full['run_id']}/recollect", json=body, headers={"Idempotency-Key": "rc-none"}), 409, "nothing_to_recollect")
    assert e["error"]["details"]["unextracted_by_reason"] == {}
    err(svc.client.post(f"/v1/runs/{full['run_id']}/recollect", json=body), 400, "idempotency_key_required")
    err(svc.client.post("/v1/runs/run_unknown1/recollect", json=body, headers={"Idempotency-Key": "rc-x"}), 404, "run_not_found")
    err(svc.client.post(f"/v1/runs/{full['run_id']}/recollect", json=dict(body, reasons=["bogus"]), headers={"Idempotency-Key": "rc-y"}), 422, "validation_failed")
