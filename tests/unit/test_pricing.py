"""The product-first sample, price ranges, price status, the default currency and the
mapping-test failures, on synthetic pages modelled on typical WooCommerce pages."""

from __future__ import annotations

import re

from app.core.csvout import PRICE_COLUMNS, record_columns
from app.core.schemas import validator
from app.extraction import variants as V
from app.extraction.distill import distill, sample_doc, variant_pointers
from app.extraction.mapping import apply_mapping
from app.extraction.page import Page
from app.extraction.pricing import DefaultCurrency, PriceRange, annotate, page_signal, price_view
from app.extraction.transforms import coerce, currency, price
from app.setup_ops import _price_and_variant_failures
from tests.fixture import variant_pages as vp

BASE = "http://variants.test:8602"
NAIVE = {"schema_version": 1, "template_id": "product", "match": {"url_patterns": ["/v/"]}, "fields": [
    {"name": "title", "type": "text", "required": True, "sources": [{"kind": "css", "selector": "h1.product_title", "extract": "text"}]},
    {"name": "price", "type": "price", "sources": [{"kind": "css", "selector": ".summary p.price", "extract": "text"}]},
    {"name": "currency", "type": "currency", "sources": [
        {"kind": "css", "selector": ".summary p.price", "extract": "text", "transforms": [{"op": "currency", "default_currency": "EUR"}]}]}]}


def _page(kind: str, maker, slug: str = "x") -> Page:
    return Page(maker(slug), f"{BASE}/v/{kind}/{slug}")


def test_price_transform_reads_ranges_and_the_first_amount():
    got = price("24,00 € – 54,00 €")
    assert isinstance(got, PriceRange) and float(got) == 24.0 and got.max_value == 54.0
    got = price("24,00 € – 54,00 € Price range: 24,00 € through 54,00 € (-31%)")
    assert isinstance(got, PriceRange) and (float(got), got.max_value) == (24.0, 54.0)
    assert price("from 10 EUR to 20 EUR").max_value == 20.0
    assert price("24,00 € 35,00 €") == 24.0 and not isinstance(price("24,00 € 35,00 €"), PriceRange)  # a sale: the first amount
    assert price("1 299,00 €") == 1299.0 and price("1.299,00 € - 10 %") == 1299.0  # a discount mark is no range
    assert not isinstance(price("54,00 € – 24,00 €"), PriceRange)  # the second side must be higher
    assert price("54 €") == 54.0 and price("Price on request") is None
    # the type keeps the range through the final coercion
    assert isinstance(coerce(PriceRange(1.0, 2.0), "price", None), PriceRange)
    assert isinstance(coerce("10,00 € – 20,00 €", "price", None), PriceRange)


def test_currency_default_is_marked():
    assert currency("24,00 €", "USD") == "EUR" and not isinstance(currency("24,00 €", "USD"), DefaultCurrency)
    got = currency("24,00", "EUR")
    assert got == "EUR" and isinstance(got, DefaultCurrency)
    assert isinstance(coerce(got, "currency", None), DefaultCurrency)
    assert currency("24,00") is None


def test_distilled_sample_gives_the_product_first():
    html = vp.woo_heavy("lens")
    page = Page(html, f"{BASE}/v/woo-heavy/lens")
    sources = V.detect_sources(page)
    assert [(s["source"]["kind"], s["count"]) for s in sources] == [("embedded_json", 3)]
    # in page order (before the product-first order) the header and mega menu alone fill the 64 KB budget before the product
    full, _ = distill(html, 10**7)
    rest = full[full.find("wss:section rest-of-page"):]
    assert len(rest[: rest.find("wss: section given above")].encode()) > 65536
    text, truncated = distill(html, 65536, variant_pointers(sources))
    assert truncated  # the rest of the page is still cut, but after the product
    assert text.startswith("<!-- wss:section product-summary at ") and "div.summary.entry-summary -->" in text[:400]
    head = text[:8000]
    assert "Price range: 24,00 € through 54,00 €" in head and "BMW-F10-KP-2009-2017" in text[:20000]
    assert 'name="attribute_pa_lens-cover-markings"' in head and '<option value="eu" class="attached enabled">EU</option>' in head and '<label for="pa_lens-cover-markings">' in head
    m = re.search(r"data-product_variations='([^']*)'", head)
    assert m and m.group(1).endswith("characters cut; parsed in variant_sources[0]]") and len(m.group(1)) < 260
    order = [text.find(k) for k in ("wss:section product-summary", "wss:section main-content", "wss: product summary, given above",
                                     "tab-description", "wss:section rest-of-page")]
    assert order == sorted(order) and -1 not in order, order
    assert text.count("Price range") == 1  # the summary is not repeated in the main content
    # a page without a summary or main region stays in page order, without section comments
    plain, _ = distill("<html><body><div><h1>A</h1><p data-x='" + "y" * 500 + "'>b</p></div></body></html>", 65536)
    assert plain.startswith("<div><h1>A</h1>") and "wss:" not in plain and "[500 characters cut]" in plain


def test_sample_doc_carries_price_on_page_and_variant_sources():
    page = _page("woo-heavy", vp.woo_heavy)
    doc = sample_doc(sample_id="smp_" + "0" * 26, page=page, requested_url=None, source_key=None, fetched_at="2026-11-14T00:00:00Z",
                     expires_at="2026-11-15T00:00:00Z", fetch={"fetch_mode": "http", "browser": None, "proxy_tier": None, "passed_at_step": None}, matched_template_id=None, suggested_template_id="v",
                     structured_types=[], max_bytes=65536, extracted=None)
    assert doc["price_on_page"] == "range" and doc["variant_sources"][0]["count"] == 3
    assert "parsed in variant_sources[0]" in doc["distilled_html"]
    errors = list(validator("samples.schema.json#/$defs/sample").iter_errors(doc))
    assert not errors, errors


def test_price_on_page_signals():
    assert page_signal(_page("woo-heavy", vp.woo_heavy)) == "range"
    assert page_signal(_page("on-request", vp.woo_on_request)) == "on_request"  # the header's mini cart 0,00 € does not count
    assert page_signal(_page("range", vp.range_page)) == "range"
    assert page_signal(_page("micro", vp.micro_page)) == "shown"
    assert page_signal(_page("release", vp.release_page)) == "shown"
    assert page_signal(Page("<html><body><main><h1>Lamp</h1></main></body></html>", f"{BASE}/a")) == "missing"
    assert page_signal(Page('<html><head><script type="application/ld+json">{"@type":"Product","name":"x","offers":{"@type":"AggregateOffer",'
                            '"lowPrice":"10","highPrice":"20","priceCurrency":"EUR"}}</script></head><body></body></html>', f"{BASE}/b")) == "range"


def _record(maker, kind):
    page = _page(kind, maker)
    data = apply_mapping(NAIVE, page).data
    return data, annotate(NAIVE, data, price_view(page, NAIVE))


def test_record_price_status_range_request_and_currency():
    data, keys = _record(vp.woo_heavy, "woo-heavy")
    assert (data["price"], data["currency"]) == (24.0, "EUR") and keys == {"price_status": "range", "price_max": 54.0}
    assert type(data["price"]) is float and type(data["currency"]) is str
    data, keys = _record(vp.woo_on_request, "on-request")
    assert data["price"] is None and data["currency"] is None and keys == {"price_status": "on_request"}  # no default EUR without a price
    data, keys = _record(vp.range_page, "range")
    assert (data["price"], keys["price_status"], keys["price_max"]) == (10.0, "range", 20.0)
    # the default stays only when the page names no currency at all, and the record says so
    page = Page("<html><body><div class='product'><h1 class='product_title'>Lamp</h1><div class='summary entry-summary'>"
                "<p class='price'><span class='amount'>24,00</span></p></div></div></body></html>", f"{BASE}/v/c")
    data = apply_mapping(NAIVE, page).data
    keys = annotate(NAIVE, data, price_view(page, NAIVE))
    assert (data["price"], data["currency"]) == (24.0, "EUR") and keys == {"price_status": "shown", "currency_from_default": True}
    # a currency the page names elsewhere replaces the default
    page = Page("<html><head><meta property='product:price:currency' content='GBP'></head><body><div class='product'><h1 class='product_title'>L</h1>"
                "<div class='summary'><p class='price'>24.00</p></div></div></body></html>", f"{BASE}/v/d")
    data = apply_mapping(NAIVE, page).data
    keys = annotate(NAIVE, data, price_view(page, NAIVE))
    assert data["currency"] == "GBP" and "currency_from_default" not in keys
    # a template without a price field: no status, nothing looked at
    no_price = {"template_id": "t", "fields": [{"name": "title", "type": "text", "sources": []}]}
    assert annotate(no_price, {"title": "x"}, price_view(page, no_price)) == {}


def test_records_validate_with_price_keys():
    rec = {"record_id": "rec_" + "a" * 32, "source_url": BASE + "/v/range/x", "site": "variants.test:8602", "template_id": "product",
           "mapping_version": 1, "item_index": 0, "price_status": "range", "price_max": 20.0, "regular_price": 25.0, "on_sale": True,
           "currency_from_default": True,
           "extracted_at": "2026-11-14T00:00:00Z", "data": {"price": 10.0}}
    assert not list(validator("record.schema.json").iter_errors(rec))
    assert list(validator("record.schema.json").iter_errors(dict(rec, price_status="free")))
    cols = record_columns([rec, {"data": {"title": "x"}}])
    i = cols.index("extracted_at") + 1
    assert cols[i: i + len(PRICE_COLUMNS)] == PRICE_COLUMNS == ["price_status", "price_max", "regular_price", "on_sale", "currency_from_default"]
    assert not set(PRICE_COLUMNS) & set(record_columns([{"data": {"title": "x"}}]))


def test_mapping_test_failures():
    checks = {"s1": {"signal": "range", "has_price": False, "variant_data": True},
              "s2": {"signal": "on_request", "has_price": False, "variant_data": False},
              "s3": {"signal": "shown", "has_price": True, "variant_data": False}}
    got = _price_and_variant_failures(NAIVE, 0, ["s1", "s2", "s3"], checks)
    assert [(f["field"], f["code"]) for f in got] == [("mappings[0].fields[1]", "price_not_extracted"), ("mappings[0].variants", "variants_not_used")]
    assert "s1" in got[0]["message"] and "s2" not in got[0]["message"] and "variant_sources" in got[1]["message"]
    # on-request pages and a mapping that uses its variants: no failure
    assert _price_and_variant_failures(dict(NAIVE, variants={"sources": []}), 0, ["s2", "s3"], checks) == []
    # no price field: the price check does not apply
    assert _price_and_variant_failures({"template_id": "t", "fields": [], "variants": {"sources": [{"kind": "jsonld"}]}}, 0, ["s1"], checks) == []
    for f in got:
        assert not list(validator("error.schema.json#/$defs/field_error").iter_errors(f))


# ---------------------------------------------------------------- split prices, separators, machine prices, sales
FIRST_BLOCK = {"schema_version": 1, "template_id": "pc", "match": {"url_patterns": ["/pc/"]}, "fields": [
    {"name": "title", "type": "text", "required": True, "sources": [{"kind": "css", "selector": "h1.product_title", "extract": "text"}]},
    {"name": "price", "type": "price", "sources": [{"kind": "css", "selector": ".summary > div > :first-child", "extract": "text"}]}]}


def _case(case: str) -> Page:
    return Page(vp.price_case(case), f"{BASE}/pc/{case}")


def test_every_price_case_reads_its_price():
    for case, (_lang, _block, want) in vp.PRICE_CASES.items():
        ext = apply_mapping(FIRST_BLOCK, _case(case))
        assert ext.data["price"] == want, (case, ext.data["price"], want)


def test_reading_rules_are_recorded():
    assert apply_mapping(FIRST_BLOCK, _case("int-sup")).notes["price"] == [{"rule": "minor_units_joined", "text": "24.99 €"}]
    assert apply_mapping(FIRST_BLOCK, _case("ambiguous-de")).notes["price"] == [{"rule": "page_language:de", "text": "1.234"}]
    assert apply_mapping(FIRST_BLOCK, _case("ambiguous-en")).notes["price"] == [{"rule": "page_language:en", "text": "1.234"}]
    assert "price" not in apply_mapping(FIRST_BLOCK, _case("de-both")).notes
    # og:locale, then the currency, when the page has no lang
    page = Page("<html><head><meta property='og:locale' content='de_DE'></head><body><div class='summary'><div><p class='price'>1.234 €</p>"
                "</div></div></body></html>", f"{BASE}/pc/x")
    ext = apply_mapping(FIRST_BLOCK | {"fields": FIRST_BLOCK["fields"][1:]}, page)
    assert ext.data["price"] == 1234.0 and ext.notes["price"][0]["rule"] == "og_locale:de_DE"
    page = Page("<html><body><div class='summary'><div><p class='price'>$1.234</p></div></div></body></html>", f"{BASE}/pc/y")
    ext = apply_mapping(FIRST_BLOCK | {"fields": FIRST_BLOCK["fields"][1:]}, page)
    assert ext.data["price"] == 1234.0 and ext.notes["price"][0]["rule"] == "default_thousands"  # "$" names no currency: no hint
    # other field types keep the plain text extraction
    text_field = {"template_id": "t", "fields": [{"name": "p", "type": "text", "sources": [{"kind": "css", "selector": ".price", "extract": "text"}]}]}
    assert apply_mapping(text_field, _case("int-sup")).data["p"] == "2499 €"


def test_sales_give_the_current_price_and_the_regular_one():
    for case, regular in vp.SALE_REGULAR.items():
        page = _case(case)
        data = apply_mapping(FIRST_BLOCK, page).data
        keys = annotate(FIRST_BLOCK, data, price_view(page, FIRST_BLOCK))
        assert (data["price"], keys.get("regular_price"), keys.get("on_sale")) == (vp.PRICE_CASES[case][2], regular, True), (case, data, keys)
    page = _case("int-sup")
    keys = annotate(FIRST_BLOCK, apply_mapping(FIRST_BLOCK, page).data, price_view(page, FIRST_BLOCK))
    assert "regular_price" not in keys and "on_sale" not in keys


def test_sale_variable_product_keeps_each_variants_regular_price():
    page = Page(vp.woo_sale_variable("x"), f"{BASE}/v/woo-sale-var/x")
    m = dict(FIRST_BLOCK, fields=[FIRST_BLOCK["fields"][0], {"name": "price", "type": "price", "sources": [
        {"kind": "css", "selector": ".summary p.price", "extract": "text"}]}],
        variants={"sources": [{"kind": "embedded_json", "selector": "form.variations_form", "attribute": "data-product_variations"}]})
    ext = apply_mapping(m, page)
    assert isinstance(ext.data["price"], PriceRange) and (float(ext.data["price"]), ext.data["price"].max_value) == (24.0, 54.0)  # current range
    variants, _info = V.resolve_in_page(page, m["variants"])
    rows = V.variant_rows(m, ext.data, variants, "embedded_json", page.base_url)
    view = price_view(page, m)
    got = [(data["price"], annotate(m, data, view)) for data, _meta, _f in rows]
    assert got == [(24.0, {"price_status": "shown", "regular_price": 30.0, "on_sale": True}),
                   (54.0, {"price_status": "shown", "regular_price": 60.0, "on_sale": True}),
                   (54.0, {"price_status": "shown", "regular_price": 60.0, "on_sale": True})], got
    assert all("\x00regular_price" not in data for data, _m, _f in rows)
    # the page record of a mapping without variants: the lowest current price, the range, the page's sale
    data = apply_mapping(FIRST_BLOCK | {"fields": m["fields"]}, page).data
    keys = annotate(m, data, view)
    assert data["price"] == 24.0 and keys == {"price_status": "range", "price_max": 54.0, "regular_price": 30.0, "on_sale": True}


def test_machine_price_and_mapping_test_price_failures():
    from app.extraction.pricing import machine_price, prices_differ

    assert machine_price(_case("mismatch")) == (19.99, "schema.org offers.price")
    assert machine_price(_case("jsonld-sale")) == (24.0, "schema.org offers.price") and machine_price(_case("int-sup")) is None
    assert prices_differ(24.99, 19.99, "x") and not prices_differ(24.99, 25.0, "x") and not prices_differ(24.99, 2499, "data-price")
    checks = {"a": {"signal": "shown", "has_price": True, "mismatch": (24.99, 19.99, "schema.org offers.price")},
              "b": {"signal": "shown", "has_price": True, "old_price": (35.0, 24.0)}}
    got = _price_and_variant_failures(NAIVE, 0, ["a", "b"], checks)
    assert [f["code"] for f in got] == ["price_mismatch", "price_is_old_price"]
    assert "mapped 24.99, the page states 19.99 (schema.org offers.price)" in got[0]["message"] and "mapped 35, current 24" in got[1]["message"]
    for f in got:
        assert not list(validator("error.schema.json#/$defs/field_error").iter_errors(f))
