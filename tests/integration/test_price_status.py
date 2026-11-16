"""End to end on the fixture shop (variants.test): the product-first sample of a heavy
WooCommerce page, `price_on_page`, the mapping test's `price_not_extracted` and
`variants_not_used`, and records with `price_status`, `price_max` and the default currency."""

from __future__ import annotations

import csv
import io
import json

from app.core.schemas import validator
from tests.conftest import check, ensure_mappings, fast, merged_config, profile, start

V = "http://variants.test:8602"
SITE = "variants.test:8602"
HEAVY = f"{V}/v/woo-heavy/bmw-f10-left"
ON_REQUEST = f"{V}/v/on-request/x5-e70"
RANGE = f"{V}/v/range/strip-5m"
PATTERN = {"url_patterns": ["/v/(woo-heavy|on-request|range)/"]}


def _mapping(price_selector: str, variants: bool) -> dict:
    m = {"schema_version": 1, "template_id": "priced", "match": PATTERN, "fields": [
        {"name": "title", "type": "text", "required": True, "sources": [{"kind": "css", "selector": "h1.product_title", "extract": "text"}]},
        {"name": "price", "type": "price", "sources": [{"kind": "css", "selector": price_selector, "extract": "text"}]},
        {"name": "currency", "type": "currency", "sources": [
            {"kind": "css", "selector": price_selector, "extract": "text", "transforms": [{"op": "currency", "default_currency": "EUR"}]}]},
        {"name": "description", "type": "text", "sources": [{"kind": "css", "selector": "#tab-description p", "extract": "text"},
                                                            {"kind": "jsonld", "schema_type": "WebPage", "path": "description"}]}]}
    if variants:
        m["variants"] = {"sources": [{"kind": "embedded_json", "selector": "form.variations_form", "attribute": "data-product_variations",
                                      "format": "woocommerce"}],
                         "fields": [{"name": "price", "path": "price"}, {"name": "markings", "option": "Type"}]}
    return m


def _test(service, ids, mapping):
    return check(service.post("/v1/mappings/test", {"site": SITE, "mappings": [mapping], "sample_ids": ids, "required_fill_threshold": 0.9}),
                 "mapping_test.schema.json#/$defs/response", 200)


def test_samples_price_on_page_and_mapping_test_failures(service, fixture_world):
    cfg = merged_config(service, {"site_url": V + "/", "fetch_mode": "http"})
    body = check(service.post("/v1/samples", {"config": cfg, "profile": profile(), "urls": [HEAVY, ON_REQUEST, RANGE]}), "samples.schema.json", 200)
    by_url = {s["url"]: s for s in body["samples"]}
    assert {u: by_url[u]["price_on_page"] for u in (HEAVY, ON_REQUEST, RANGE)} == {HEAVY: "range", ON_REQUEST: "on_request", RANGE: "range"}
    heavy = by_url[HEAVY]
    assert heavy["distilled_html_truncated"] and heavy["distilled_html"].startswith("<!-- wss:section product-summary at ")
    head = heavy["distilled_html"][:8000]
    assert "Price range: 24,00 € through 54,00 €" in head and "parsed in variant_sources[0]]" in head and '<option value="usa"' in head
    assert heavy["variant_sources"][0]["source"]["attribute"] == "data-product_variations" and heavy["variant_sources"][0]["count"] == 3
    ids = [by_url[u]["sample_id"] for u in (HEAVY, ON_REQUEST, RANGE)]

    # no variants declared → variants_not_used fails the test although every required field is filled
    got = _test(service, ids, _mapping(".summary p.price", variants=False))
    t = got["templates"][0]
    assert not got["passed"] and not t["passed"] and [f["code"] for f in t["failures"]] == ["variants_not_used"], t
    assert t["failures"][0]["field"] == "mappings[0].variants" and any(w["code"] == "variants_not_used" for w in got["warnings"])
    per = {s["url"]: s for s in got["samples"]}
    assert (per[RANGE]["data"]["price"], per[RANGE]["price_status"], per[RANGE]["price_max"]) == (10.0, "range", 20.0)
    assert per[ON_REQUEST]["price_status"] == "on_request" and per[ON_REQUEST]["data"]["currency"] is None  # no default EUR without a price
    assert per[HEAVY]["data"]["description"].startswith("The BMW 5 F10 F11 LCI")  # the product's own text before the WebPage text

    # a price source that reads nothing → price_not_extracted on the pages that show a price, not the on-request page
    got = _test(service, ids, _mapping(".summary .nope", variants=True))
    t = got["templates"][0]
    codes = [f["code"] for f in t["failures"]]
    assert not t["passed"] and codes == ["price_not_extracted"], t
    msg = t["failures"][0]["message"]
    assert by_url[RANGE]["sample_id"] in msg and by_url[ON_REQUEST]["sample_id"] not in msg
    assert by_url[HEAVY]["sample_id"] not in msg  # its variants carry the prices

    # variants used, price read → passes; every variant has its own price
    got = _test(service, ids, _mapping(".summary p.price", variants=True))
    t = got["templates"][0]
    assert got["passed"] and t["passed"] and t["failures"] == [], t
    items = next(s for s in got["samples"] if s["url"] == HEAVY)["variants"]["items"]
    assert [(i["data"]["price"], i["data"]["markings"], i["price_status"]) for i in items] == [(24.0, "No", "shown"), (54.0, "EU", "shown"), (54.0, "USA", "shown")]


def test_job_records_carry_price_status(service, fixture_world):
    ensure_mappings(service, SITE, _mapping(".summary p.price", variants=True))
    cfg = merged_config(service, {"discovery_mode": "url_list", "urls": [{"url": HEAVY}, {"url": ON_REQUEST}, {"url": RANGE}], "fetch_mode": "http",
                                  **fast(max_concurrency=1)})
    run = service.wait_run(start(service, cfg, "price-status-job").json()["run_id"])
    assert run["counters"]["records_extracted"] == 5, run["counters"]
    lines = [json.loads(line) for line in service.get(run["result"]["download_path"]).text.splitlines()]
    for rec in lines:
        assert not list(validator("record.schema.json").iter_errors(rec)), rec
    by = sorted(((r["source_url"].split("/")[4], r["item_index"]), r) for r in lines)
    got = [(k, r["data"]["price"], r["data"]["currency"], r["price_status"], r.get("price_max"), r.get("currency_from_default")) for k, r in by]
    assert got == [(("on-request", 0), None, None, "on_request", None, None),
                   (("range", 0), 10.0, "EUR", "range", 20.0, None),
                   (("woo-heavy", 0), 24.0, "EUR", "shown", None, None),
                   (("woo-heavy", 1), 54.0, "EUR", "shown", None, None),
                   (("woo-heavy", 2), 54.0, "EUR", "shown", None, None)], got
    rows = list(csv.DictReader(io.StringIO(service.get(run["result"]["download_path"], params={"format": "csv"}).content.decode("utf-8-sig")),
                               delimiter=";"))
    assert sorted(r["price_status"] for r in rows) == ["on_request", "range", "shown", "shown", "shown"]
    assert next(r for r in rows if r["price_status"] == "range")["price_max"] == "20.0"


GC = f"{V}/gc/"
SIMPLE = {"schema_version": 1, "template_id": "gc", "match": {"url_patterns": ["/gc/[a-z][0-9]+$"]}, "fields": [
    {"name": "title", "type": "text", "required": True, "sources": [{"kind": "css", "selector": "h1.product_title", "extract": "text"}]},
    {"name": "price", "type": "price", "sources": [{"kind": "css", "selector": ".summary .regular-price", "extract": "text"}]},
    {"name": "currency", "type": "currency", "sources": [{"kind": "css", "selector": ".summary .regular-price", "extract": "text"}]},
    {"name": "sku", "type": "text", "sources": [{"kind": "css", "selector": ".sku", "extract": "text"}]}]}
FIXED = {**SIMPLE, "fields": [
    SIMPLE["fields"][0],
    {"name": "price", "type": "price", "sources": [{"kind": "css", "selector": ".summary .regular-price", "extract": "text"},
                                                   {"kind": "css", "selector": ".summary p.price ins", "extract": "text"},
                                                   {"kind": "css", "selector": ".summary p.price", "extract": "text"}]},
    {"name": "currency", "type": "currency", "sources": [{"kind": "css", "selector": ".summary p.price", "extract": "text"}]},
    SIMPLE["fields"][3]],
    "variants": {"sources": [{"kind": "embedded_json", "selector": "form.variations_form", "attribute": "data-product_variations", "format": "woocommerce"}],
                 "fields": [{"name": "price", "path": "price"}, {"name": "markings", "option": "Type"}]}}


def _wait(service, run_id, pred, timeout=120):
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        run = service.get(f"/v1/runs/{run_id}").json()
        if pred(run):
            return run
        time.sleep(0.3)
    raise AssertionError(json.dumps(run)[:4000])


def test_variable_products_later_in_a_crawl_open_a_repair(service, fixture_world):
    """A mapping built from simple products meets variable ones (and a sale price it does not read) later in the
    crawl: those pages wait for a repair instead of becoming records with a null price; a price on request does not."""
    ensure_mappings(service, SITE, SIMPLE)
    cfg = merged_config(service, {"site_url": GC, "discovery_mode": "crawl", "fetch_mode": "http", **fast(max_concurrency=1)})
    run_id = start(service, cfg, "gap-crawl").json()["run_id"]
    try:
        _repair_flow(service, run_id)
    finally:
        if service.get(f"/v1/runs/{run_id}").json()["state"] not in ("completed", "failed"):
            service.post(f"/v1/runs/{run_id}/stop", {})  # never leave the site busy for the next test (one run per site)
            service.wait_run(run_id)


def _repair_flow(service, run_id):
    # every page of the catalogue fetched (listing + 9 products) and the repair open
    run = _wait(service, run_id, lambda r: r["progress"]["open_mapping_requests"] == 1 and r["progress"]["queue_pending"] == 0
                and r["progress"]["in_progress"] == 0 and sum(r["counters"]["pages_by_class"].values()) >= 10)
    assert run["state"] == "running" and run["counters"]["records_extracted"] == 5, run["counters"]  # p1-p4 and r1 (price on request)
    unex = {i["url"].rsplit("/", 1)[-1]: i for i in service.get(f"/v1/runs/{run_id}/unextracted", params={"limit": 200}).json()["items"]}
    assert set(unex) == {"s1", "v1", "v2", "v3"}, unex
    assert unex["s1"]["reason"] == "incomplete" and unex["s1"]["mapping_gaps"] == ["price_not_extracted"]
    assert unex["v1"]["mapping_gaps"] == ["variants_not_used", "price_not_extracted"]
    assert "declares no variants" in unex["v1"]["last_error"] and "kept for a repair" in unex["v1"]["last_error"]
    for item in unex.values():
        assert not list(validator("run_summary.schema.json#/$defs/unextracted_page").iter_errors(item)), item
    mrq_id = next(e for e in run["events"] if e["event_type"] == "mapping_required")["resource_id"]
    mrq = check(service.get(f"/v1/mapping-requests/{mrq_id}"), "mapping_request.schema.json", 200)
    assert mrq["reason"] == "incomplete" and mrq["template_id"] == "gc"
    assert mrq["failures"]["gaps"]["variants_not_used"] >= 2 and mrq["failures"]["gaps"]["price_not_extracted"] >= 3, mrq["failures"]
    assert any(s["variant_sources"] for s in mrq["samples"]) and mrq["samples"][0]["url"].rsplit("/", 1)[-1] in ("s1", "v1", "v2", "v3")
    ids = [s["sample_id"] for s in mrq["samples"]]
    bad = _test(service, ids, SIMPLE)
    assert not bad["passed"] and {f["code"] for f in bad["templates"][0]["failures"]} == {"variants_not_used", "price_not_extracted"}
    good = _test(service, ids, FIXED)
    assert good["passed"], good["templates"][0]
    act = service.put(f"/v1/sites/{SITE}/mappings", {"template_id": "gc", "expected_version": mrq["expected_version"], "mapping": FIXED,
                                                     "origin": {"run_id": run_id, "mapping_request_id": mrq_id}})
    assert act.status_code == 200, act.text
    run = service.wait_run(run_id)
    assert run["state"] == "completed" and run["outcome"] == "complete", run["counters"]
    lines = [json.loads(line) for line in service.get(run["result"]["download_path"]).text.splitlines()]
    by = {(r["source_url"].rsplit("/", 1)[-1], r["item_index"]): r for r in lines}
    assert len(lines) == 15, sorted(by)  # p1-p4, r1, s1 and 3 × 3 variants
    assert (by[("r1", 0)]["data"]["price"], by[("r1", 0)]["price_status"], by[("r1", 0)]["data"]["currency"]) == (None, "on_request", None)
    assert (by[("s1", 0)]["data"]["price"], by[("s1", 0)]["price_status"]) == (11.0, "shown")
    assert [by[("v2", i)]["data"]["price"] for i in range(3)] == [24.0, 54.0, 54.0] and by[("v2", 1)]["data"]["markings"] == "EU"
    assert service.get(f"/v1/runs/{run_id}/unextracted", params={"limit": 200}).json()["items"] == []


def test_gaps_without_a_repair_keep_their_records(service, fixture_world):
    """No repair possible (max_repairs_per_template 0): the pages are extracted as they are, nothing is lost."""
    ensure_mappings(service, SITE, SIMPLE)
    cfg = merged_config(service, {"site_url": GC, "discovery_mode": "crawl", "fetch_mode": "http", "max_repairs_per_template": 0,
                                  **fast(max_concurrency=1)})
    run = service.wait_run(start(service, cfg, "gap-crawl-norepair").json()["run_id"])
    assert run["state"] == "completed" and run["counters"]["records_extracted"] == 9, run["counters"]
    assert service.get(f"/v1/runs/{run['run_id']}/unextracted", params={"limit": 200}).json()["items"] == []


PC = {case: f"{V}/pc/{case}" for case in ("int-sup", "ambiguous-de", "mismatch", "woo-sale", "magento-sale")}
PRICED = {"schema_version": 1, "template_id": "pc", "match": {"url_patterns": ["/pc/"]}, "fields": [
    {"name": "title", "type": "text", "required": True, "sources": [{"kind": "css", "selector": "h1.product_title", "extract": "text"}]},
    {"name": "price", "type": "price", "sources": [{"kind": "css", "selector": ".summary > div > :first-child", "extract": "text"}]}]}


def test_split_prices_sales_and_machine_prices(service, fixture_world):
    cfg = merged_config(service, {"site_url": V + "/", "fetch_mode": "http"})
    body = check(service.post("/v1/samples", {"config": cfg, "profile": profile(), "urls": list(PC.values())}), "samples.schema.json", 200)
    by = {s["url"].rsplit("/", 1)[-1]: s for s in body["samples"]}
    assert by["mismatch"]["price_details"] == {"machine_price": 19.99, "machine_source": "schema.org offers.price"}
    assert by["woo-sale"]["price_details"] == {"current": 24.0, "regular_price": 35.0, "on_sale": True, "sale_source": "struck-through price"}
    ids = [by[c]["sample_id"] for c in PC]
    got = _test(service, ids, PRICED)
    t = got["templates"][0]
    assert [f["code"] for f in t["failures"]] == ["price_mismatch"] and by["mismatch"]["sample_id"] in t["failures"][0]["message"], t["failures"]
    per = {s["url"].rsplit("/", 1)[-1]: s for s in got["samples"]}
    assert per["int-sup"]["data"]["price"] == 24.99 and per["ambiguous-de"]["data"]["price"] == 1234.0
    assert (per["woo-sale"]["data"]["price"], per["woo-sale"]["regular_price"], per["woo-sale"]["on_sale"]) == (24.0, 35.0, True)
    assert (per["magento-sale"]["data"]["price"], per["magento-sale"]["regular_price"]) == (39.9, 49.9)
    rules = {(r["sample_id"], r["rule"]) for r in next(f for f in t["fields"] if f["name"] == "price")["reading_rules"]}
    assert (by["int-sup"]["sample_id"], "minor_units_joined") in rules and (by["ambiguous-de"]["sample_id"], "page_language:de") in rules
    # a mapping that reads the first amount of the block (the struck-through one on a sale page)
    first_amount = dict(PRICED, fields=[PRICED["fields"][0], {"name": "price", "type": "price", "sources": [
        {"kind": "css", "selector": ".summary .woocommerce-Price-amount, .summary .price-box .price", "extract": "text"}]}])
    got = _test(service, [by["woo-sale"]["sample_id"], by["magento-sale"]["sample_id"]], first_amount)
    t = got["templates"][0]
    assert [f["code"] for f in t["failures"]] == ["price_is_old_price"] and "mapped 35, current 24" in t["failures"][0]["message"], t["failures"]


def test_sale_records_carry_the_regular_price(service, fixture_world):
    sale_var = f"{V}/v/woo-sale-var/lens"
    mapping = dict(PRICED, template_id="pcsale", match={"url_patterns": ["/pc/woo-sale", "/v/woo-sale-var/"]})
    var = dict(mapping, template_id="salevar", match={"url_patterns": ["/v/woo-sale-var/"]}, fields=[PRICED["fields"][0], {
        "name": "price", "type": "price", "sources": [{"kind": "css", "selector": ".summary p.price", "extract": "text"}]}],
        variants={"sources": [{"kind": "embedded_json", "selector": "form.variations_form", "attribute": "data-product_variations"}]})
    mapping["match"] = {"url_patterns": ["/pc/woo-sale"]}
    ensure_mappings(service, SITE, mapping, var)
    cfg = merged_config(service, {"discovery_mode": "url_list", "urls": [{"url": PC["woo-sale"]}, {"url": sale_var}], "fetch_mode": "http",
                                  **fast(max_concurrency=1)})
    run = service.wait_run(start(service, cfg, "sale-records").json()["run_id"])
    lines = [json.loads(line) for line in service.get(run["result"]["download_path"]).text.splitlines()]
    for rec in lines:
        assert not list(validator("record.schema.json").iter_errors(rec)), rec
    got = sorted((r["template_id"], r["item_index"], r["data"]["price"], r.get("regular_price"), r.get("on_sale")) for r in lines)
    assert got == [("pcsale", 0, 24.0, 35.0, True), ("salevar", 0, 24.0, 30.0, True), ("salevar", 1, 54.0, 60.0, True),
                   ("salevar", 2, 54.0, 60.0, True)], got
    rows = list(csv.DictReader(io.StringIO(service.get(run["result"]["download_path"], params={"format": "csv"}).content.decode("utf-8-sig")),
                               delimiter=";"))
    assert sorted(r["regular_price"] for r in rows) == ["30.0", "35.0", "60.0", "60.0"] and {r["on_sale"] for r in rows} == {"true"}
