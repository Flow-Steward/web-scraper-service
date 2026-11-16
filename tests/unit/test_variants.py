"""Variants from data and microdata, on the synthetic shop pages."""

from __future__ import annotations

import pytest

from app.core.schemas import validator
from app.extraction import variants as V
from app.extraction.mapping import apply_mapping
from app.extraction.page import Page
from tests.fixture import variant_pages as vp

BASE = "http://variants.test:8602"


def _page(kind: str, slug: str, maker) -> Page:
    return Page(maker(slug), f"{BASE}/v/{kind}/{slug}")


def test_woocommerce_variations_form_with_labels_from_the_form():
    page = _page("woo", "bmw-f10-left", vp.woo_classic)
    got = V.from_embedded(page, {"kind": "embedded_json", "selector": "form.variations_form", "attribute": "data-product_variations"})
    assert [v["options"] for v in got] == [{"Lens cover markings": "No"}, {"Lens cover markings": "EU sticker"}, {"Lens cover markings": "USA sticker"}]
    assert [v["price"] for v in got] == [24.0, 54.0, 54.0] and [v["id"] for v in got] == [47833, 47834, 47835]
    assert {v["sku"] for v in got} == {"BMW-F10-KP-2009-2017"}  # the parent SKU on every variation: identity is the position/id, not sku
    assert got[1]["image"] == "/wp-content/uploads/lens-eu.jpg" and got[1]["availability"] == "InStock" and got[1]["description"] == "Lens cover, EU marking."
    found = V.detect_sources(page)
    assert found[0]["source"]["kind"] == "embedded_json" and found[0]["count"] == 3 and found[0]["option_names"] == ["Lens cover markings"]
    assert V.page_platforms(page) == ["woocommerce"] and V.woocommerce_product_id(page) == "6689"


async def test_shopify_product_json_minor_units_grams_barcode():
    page = _page("products", "big-bad-back-ep", vp.shopify_page)
    asked = []

    async def fetch(url):
        asked.append(url)
        return vp.SHOPIFY_PRODUCT

    got = await V.from_shop_json(page, {"kind": "shop_json", "platform": "shopify", "currency": "GBP"}, fetch)
    assert asked == [f"{BASE}/products/big-bad-back-ep.js"]
    assert [(v["options"], v["price"], v["currency"], v["sku"], v["barcode"], v["weight"], v["availability"]) for v in got] == [
        ({"Format": "CD [BB1CD]"}, 10.99, "GBP", "BB1CD", "5037300088600", "52 g", "InStock"),
        ({"Format": '12" [BB1LP]'}, 17.99, "GBP", "BB1LP", "5037300088617", "208 g", "OutOfStock")]
    # the theme's JSON block in the page gives the same variants without a request
    inpage = V.from_embedded(page, {"kind": "embedded_json", "selector": "script#ProductJson-main", "path": "variants"})
    assert [v["price"] for v in inpage] == [10.99, 17.99] and inpage[0]["options"] == {"Format": "CD [BB1CD]"}
    assert V.page_platforms(page) == ["shopify"]


async def test_woocommerce_store_api_two_requests():
    page = _page("woo-block", "headlight-glass", vp.woo_block)
    asked = []

    async def fetch(url):
        asked.append(url)
        if url.endswith("/products/7001"):
            return vp.STORE_PRODUCT
        return [v for v in vp.STORE_VARIATIONS if str(v["id"]) in url.split("include=")[1].split("&")[0].split(",")]

    got = await V.from_shop_json(page, {"kind": "shop_json", "platform": "woocommerce"}, fetch)
    assert asked == [f"{BASE}/wp-json/wc/store/v1/products/7001", f"{BASE}/wp-json/wc/store/v1/products?type=variation&include=7101,7102,7103&per_page=100"]
    assert [(v["options"], v["price"], v["currency"], v["sku"], v["in_stock"]) for v in got] == [
        ({"Sticker": "US sticker"}, 49.0, "EUR", "HG-7001-US", True), ({"Sticker": "EU sticker"}, 45.0, "EUR", "HG-7001-EU", True),
        ({"Sticker": "no sticker"}, 39.0, "EUR", "HG-7001-NONE", False)]


def test_jsonld_offers_and_has_variant_and_microdata():
    rel = _page("release", "x1", vp.release_page)
    got = V.from_structured(rel, {"kind": "jsonld", "schema_type": "Product", "path": "offers"})
    assert [(v["options"], v["price"], v["currency"], v["availability"]) for v in got] == [
        ({"format": "CD"}, 12.99, "EUR", "InStock"), ({"format": "Vinyl"}, 24.99, "EUR", "OutOfStock")]
    grp = _page("group", "tee", vp.group_page)
    got = V.from_structured(grp, {"kind": "jsonld", "schema_type": "ProductGroup", "path": "hasVariant"})
    assert [v["options"] for v in got] == [{"size": "S", "color": "red"}, {"size": "M", "color": "red"}, {"size": "M", "color": "blue"}]
    assert got[2]["price"] == 21.0 and got[2]["sku"] == "tee-M-blue"
    kinds = {d["source"]["path"] for d in V.detect_sources(rel) + V.detect_sources(grp)}
    assert kinds == {"offers", "hasVariant"}
    micro = _page("micro", "m1", vp.micro_page)
    assert micro.microdata_types() == ["Product", "Offer"]
    mapping = {"schema_version": 1, "template_id": "lamp", "match": {"url_patterns": ["/micro/"]}, "fields": [
        {"name": "title", "type": "text", "sources": [{"kind": "microdata", "schema_type": "Product", "path": "name"}]},
        {"name": "price", "type": "price", "sources": [{"kind": "microdata", "schema_type": "Product", "path": "offers.price"}]},
        {"name": "sku", "type": "text", "sources": [{"kind": "microdata", "schema_type": "Product", "path": "sku"}]}]}
    assert not list(validator("mapping.schema.json").iter_errors(mapping))
    ext = apply_mapping(mapping, micro)
    assert ext.data == {"title": "Lamp m1", "price": 89.5, "sku": "LMP-m1"}
    assert apply_mapping(mapping, micro, "structured_data").data["title"] == "Lamp m1"  # microdata counts as structured data


async def test_variant_rows_override_page_fields_and_mapping_validates():
    page = _page("woo", "bmw-f10-left", vp.woo_classic)
    mapping = {"schema_version": 1, "template_id": "lens", "match": {"url_patterns": ["/woo/"]}, "fields": [
        {"name": "title", "type": "text", "required": True, "sources": [{"kind": "css", "selector": "h1.product_title", "extract": "text"}]},
        {"name": "price", "type": "price", "sources": [{"kind": "css", "selector": "p.price .amount", "extract": "text"}]},
        {"name": "sku", "type": "text", "sources": [{"kind": "css", "selector": ".sku", "extract": "text"}]}],
        "variants": {"sources": [{"kind": "shop_json", "platform": "woocommerce"},
                                 {"kind": "embedded_json", "selector": "form.variations_form", "attribute": "data-product_variations", "format": "woocommerce"}],
                     "fields": [{"name": "price", "path": "price"}, {"name": "markings", "option": "lens cover markings"},
                                {"name": "variant_id", "path": "id"}, {"name": "image", "path": "image", "type": "image_url"}]}}
    assert not list(validator("mapping.schema.json").iter_errors(mapping))

    async def no_store_api(url):
        return None

    variants, info = await V.resolve(page, mapping["variants"], no_store_api)
    assert info == {"kind": "embedded_json", "index": 1}  # the Store API gave nothing: the in-page source
    ext = apply_mapping(mapping, page)
    rows = V.variant_rows(mapping, ext.data, variants, info["kind"], page.url)
    assert [r[0]["price"] for r in rows] == [24.0, 54.0, 54.0] and [r[0]["markings"] for r in rows] == ["No", "EU sticker", "USA sticker"]
    assert rows[0][0]["title"] == "BMW lens cover bmw-f10-left" and rows[2][0]["variant_id"] == "47835"
    assert rows[1][0]["image"] == f"{BASE}/wp-content/uploads/lens-eu.jpg"
    assert rows[1][1] == {"index": 1, "count": 3, "source": "embedded_json", "options": {"Lens cover markings": "EU sticker"}, "id": "47834"}
    record = {"record_id": "rec_" + "0" * 32, "source_url": page.url, "site": "variants.test:8602", "template_id": "lens", "mapping_version": 1,
              "item_index": 1, "variant": rows[1][1], "extracted_at": "2026-11-13T12:00:00Z", "data": rows[1][0]}
    assert not list(validator("record.schema.json").iter_errors(record))
    # no variants: one row without variant meta, the variant-only fields present as null
    single = V.variant_rows(mapping, ext.data, [], None, page.url)
    assert len(single) == 1 and single[0][1] is None and single[0][0]["markings"] is None


@pytest.mark.parametrize("bad", [
    {"kind": "embedded_json"},  # selector required
    {"kind": "shop_json"},  # platform required
    {"kind": "jsonld", "schema_type": "Product"},  # path required
    {"kind": "shop_json", "platform": "magento"},
])
def test_variant_source_schema_refuses(bad):
    mapping = {"schema_version": 1, "template_id": "x", "match": {"url_patterns": ["/"]},
               "fields": [{"name": "title", "type": "text", "sources": [{"kind": "css", "selector": "h1", "extract": "text"}]}],
               "variants": {"sources": [bad]}}
    assert list(validator("mapping.schema.json").iter_errors(mapping))
