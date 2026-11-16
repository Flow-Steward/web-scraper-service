"""End to end on the fixture shop (variants.test): samples show the variant
sources, the mapping test reports variant counts and per-variant fill, a job writes one record
per variant."""

from __future__ import annotations

import json

from tests.conftest import check, ensure_mappings, fast, merged_config, profile, start

V = "http://variants.test:8602"
SITE = "variants.test:8602"
WOO = f"{V}/v/woo/bmw-f10-left"
WOO_BLOCK = f"{V}/v/woo-block/headlight-glass"
SHOPIFY = f"{V}/v/products/big-bad-back-ep"
RELEASE = f"{V}/v/release/x1"

LENS = {"schema_version": 1, "template_id": "lens", "match": {"url_patterns": ["/v/woo/"]}, "fields": [
    {"name": "title", "type": "text", "required": True, "sources": [{"kind": "css", "selector": "h1.product_title", "extract": "text"}]},
    {"name": "price", "type": "price", "sources": [{"kind": "css", "selector": "p.price .amount", "extract": "text"}]},
    {"name": "sku", "type": "text", "sources": [{"kind": "css", "selector": ".sku", "extract": "text"}]}],
    "variants": {"sources": [{"kind": "embedded_json", "selector": "form.variations_form", "attribute": "data-product_variations", "format": "woocommerce"}],
                 "fields": [{"name": "price", "path": "price"}, {"name": "markings", "option": "Lens cover markings"}, {"name": "variant_id", "path": "id"}]}}
GLASS = {"schema_version": 1, "template_id": "glass", "match": {"url_patterns": ["/v/woo-block/"]}, "fields": [
    {"name": "title", "type": "text", "required": True, "sources": [{"kind": "css", "selector": "h1", "extract": "text"}]},
    {"name": "price", "type": "price", "sources": [{"kind": "css", "selector": ".wp-block-woocommerce-product-price", "extract": "text"}]},
    {"name": "currency", "type": "currency", "sources": [{"kind": "css", "selector": ".wp-block-woocommerce-product-price", "extract": "text"}]}],
    "variants": {"sources": [{"kind": "shop_json", "platform": "woocommerce"}],
                 "fields": [{"name": "price", "path": "price"}, {"name": "currency", "path": "currency"}, {"name": "sticker", "option": "Sticker"}]}}
RECORD = {"schema_version": 1, "template_id": "release", "match": {"url_patterns": ["/v/products/"]}, "fields": [
    {"name": "title", "type": "text", "required": True, "sources": [{"kind": "css", "selector": "h1", "extract": "text"}]},
    {"name": "price", "type": "price", "sources": [{"kind": "css", "selector": ".price", "extract": "text"}]},
    {"name": "currency", "type": "currency", "sources": [{"kind": "meta", "name": "og:price:currency"}]},
    {"name": "sku", "type": "text", "sources": [{"kind": "css", "selector": ".nope", "extract": "text"}]},
    {"name": "barcode", "type": "text", "sources": [{"kind": "css", "selector": ".nope", "extract": "text"}]},
    {"name": "weight", "type": "text", "sources": [{"kind": "css", "selector": ".nope", "extract": "text"}]}],
    "variants": {"sources": [{"kind": "shop_json", "platform": "shopify"}]}}  # default fields: price, sku, barcode, weight …


def test_samples_list_the_variant_sources(service, fixture_world):
    cfg = merged_config(service, {"site_url": V + "/", "fetch_mode": "http"})
    body = check(service.post("/v1/samples", {"config": cfg, "profile": profile(), "urls": [WOO, WOO_BLOCK, SHOPIFY, RELEASE]}),
                 "samples.schema.json", 200)
    by_url = {s["url"]: s for s in body["samples"]}
    woo = [(d["source"]["kind"], d["count"]) for d in by_url[WOO]["variant_sources"]]
    assert woo == [("embedded_json", 3)], by_url[WOO]["variant_sources"]
    assert by_url[WOO]["variant_sources"][0]["option_names"] == ["Lens cover markings"]
    block = by_url[WOO_BLOCK]["variant_sources"]
    assert [(d["source"], d["count"]) for d in block] == [({"kind": "shop_json", "platform": "woocommerce"}, 3)], block
    shop = {d["source"]["kind"]: d for d in by_url[SHOPIFY]["variant_sources"]}
    assert shop["shop_json"]["count"] == 2 and shop["embedded_json"]["source"]["selector"] == 'script[type="application/json"]#ProductJson-main'
    assert shop["shop_json"]["example"]["price"] == 10.99 and shop["shop_json"]["example"]["options"] == {"Format": "CD [BB1CD]"}
    assert [d["source"]["path"] for d in by_url[RELEASE]["variant_sources"]] == ["offers"]
    ex = body["explanation"]
    assert ex["samples_with_variant_sources"] == 4 and "variant sources:" in ex["summary"] and "WooCommerce Store API on 1 of 4" in ex["summary"]
    hits = fixture_world.state(8602)["hits"]
    assert hits.get("8602/products/big-bad-back-ep.js") == 1 and hits.get("8602/wp-json/wc/store/v1/products/7001") == 1
    pytest_ids = [s["sample_id"] for s in body["samples"]]
    # the mapping test applies the variant sources to the stored samples (Store API JSON kept with the sample)
    test = check(service.post("/v1/mappings/test", {"site": SITE, "mappings": [LENS, GLASS, RECORD], "sample_ids": pytest_ids[:3],
                                                     "required_fill_threshold": 0.9}),
                 "mapping_test.schema.json#/$defs/response", 200)
    per = {t["template_id"]: t for t in test["templates"]}
    assert per["lens"]["variant_count"] == 3 and per["glass"]["variant_count"] == 3 and per["release"]["variant_count"] == 2, per
    assert per["glass"]["variant_sources_used"] == {"shop_json": 1}
    vf = {f["name"]: f for f in per["glass"]["variant_fields"]}
    assert vf["sticker"]["fill_rate"] == 1.0 and vf["price"]["example_values"][:2] == [49.0, 45.0]
    rel = {f["name"]: f for f in per["release"]["variant_fields"]}
    assert set(rel) == {"price", "currency", "sku", "barcode", "weight"} and rel["barcode"]["fill_rate"] == 1.0 and rel["currency"]["fill_rate"] == 0.0
    sample_lens = next(s for s in test["samples"] if s["template_id"] == "lens")
    assert sample_lens["variants"]["count"] == 3 and sample_lens["variants"]["items"][1]["data"]["markings"] == "EU sticker"


def test_job_writes_one_record_per_variant(service, fixture_world):
    ensure_mappings(service, SITE, LENS, RECORD)
    fixture_world.set(8602, reset_hits=True)
    cfg = merged_config(service, {"discovery_mode": "url_list", "urls": [{"url": WOO}, {"url": SHOPIFY}], "fetch_mode": "http",
                                  **fast(max_concurrency=1)})
    run = service.wait_run(start(service, cfg, "variants-job").json()["run_id"])
    assert run["counters"]["records_extracted"] == 5, run["counters"]
    lines = [json.loads(line) for line in service.get(run["result"]["download_path"]).text.splitlines()]
    for rec in lines:
        assert not list(__import__("app.core.schemas", fromlist=["validator"]).validator("record.schema.json").iter_errors(rec)), rec
    lens = sorted((r for r in lines if r["template_id"] == "lens"), key=lambda r: r["item_index"])
    assert [r["item_index"] for r in lens] == [0, 1, 2] and len({r["record_id"] for r in lens}) == 3
    assert [r["data"]["markings"] for r in lens] == ["No", "EU sticker", "USA sticker"] and [r["data"]["price"] for r in lens] == [24.0, 54.0, 54.0]
    assert lens[1]["variant"] == {"index": 1, "count": 3, "source": "embedded_json", "options": {"Lens cover markings": "EU sticker"}, "id": "47834"}
    rel = sorted((r for r in lines if r["template_id"] == "release"), key=lambda r: r["item_index"])
    assert [(r["data"]["price"], r["data"]["sku"], r["data"]["barcode"], r["data"]["weight"]) for r in rel] == [
        (10.99, "BB1CD", "5037300088600", "52 g"), (17.99, "BB1LP", "5037300088617", "208 g")]
    assert all(r["data"]["currency"] == "GBP" for r in rel)  # the page's currency stays where the variant has none
    assert fixture_world.state(8602)["hits"].get("8602/products/big-bad-back-ep.js") == 1  # one light request per product
