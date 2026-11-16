"""Mapping application, transforms, type coercion and the fixed HTML clean-up."""

import json

import pytest

from app.extraction.htmlclean import clean_html
from app.extraction.mapping import apply_mapping, jsonld_values, pick_template, required_set
from app.extraction.page import Page
from app.extraction import transforms as T

BASE = "https://www.lumen-shop.example/produkt/aurora"
JSONLD = {
    "@context": "https://schema.org",
    "@graph": [
        {"@type": "BreadcrumbList", "itemListElement": []},
        {"@type": ["Product", "Thing"], "name": " Aurora  60 ", "sku": "AUR-60",
         "image": ["/a.jpg", {"@type": "ImageObject", "url": "https://cdn.example/b.jpg"}],
         "brand": {"@type": "Brand", "name": "Lumen"},
         "offers": [{"@type": "Offer", "price": "389.00", "priceCurrency": "EUR", "availability": "https://schema.org/InStock"},
                    {"@type": "Offer", "price": "399.00", "priceCurrency": "EUR"}],
         "weight": {"@value": "2.5"}},
    ],
}
HTML = f"""<html><head><base href="https://www.lumen-shop.example/"><title>Aurora</title>
<meta name="description" content="Desc"><meta property="og:title" content="OG Aurora">
<script type="application/ld+json">{json.dumps(JSONLD)}</script></head>
<body><div class="product-detail"><h1 class="t">Aurora 60</h1><span class="price">1.299,00 €</span>
<div class="desc"><p>Text <b>bold</b></p><ul><li>A</li></ul><table><tr><td>E27</td></tr></table>
<script>x()</script><style>p{{}}</style><form><input name="q"></form><iframe src="/x"></iframe>
<a href="/kat?utm_source=a&id=1" onclick="t()" data-gtm-x="1" data-track-id="2">link</a><img src="img/c.jpg" data-ga-z="1"></div>
<ul class="gal"><li><img data-src="/g1.jpg"></li><li><img data-src="/g2.jpg"></li></ul>
<span class="date">03.11.2026</span><span class="stock">Ja</span></div></body></html>"""


def page():
    return Page(HTML, BASE)


def test_jsonld_paths():
    p = page()
    assert jsonld_values(p, "Product", "name", False) == [" Aurora  60 "]
    assert jsonld_values(p, "Product", "offers.price", False) == ["389.00"]  # array without index -> first
    assert jsonld_values(p, "Product", "offers.price", True) == ["389.00", "399.00"]
    assert jsonld_values(p, "Product", "offers.1.price", False) == ["399.00"]
    assert jsonld_values(p, "Product", "image.*", True) == ["/a.jpg", "https://cdn.example/b.jpg"]
    assert jsonld_values(p, "Product", "brand", False) == []  # object without @value/url/@id
    assert jsonld_values(p, "Product", "weight", False) == ["2.5"]
    assert jsonld_values(p, "Offer", "price", False) == ["389.00"]  # nested node, document order


MAPPING = {
    "schema_version": 1, "template_id": "product", "match": {"url_patterns": ["/produkt/"], "selector": "div.product-detail"},
    "fields": [
        {"name": "title", "type": "text", "required": True, "sources": [{"kind": "jsonld", "schema_type": "Product", "path": "name"}]},
        {"name": "price", "type": "price", "sources": [{"kind": "css", "selector": "span.price", "extract": "text"}]},
        {"name": "currency", "type": "currency", "sources": [{"kind": "css", "selector": "span.price", "extract": "text"}]},
        {"name": "description", "type": "html", "sources": [{"kind": "css", "selector": "div.desc", "extract": "html"}]},
        {"name": "images", "type": "image_url", "multiple": True, "sources": [
            {"kind": "css", "selector": "div.nothing img", "extract": "attribute", "attribute": "src"},
            {"kind": "css", "selector": "ul.gal img", "extract": "attribute", "attribute": "data-src"}]},
        {"name": "og", "type": "text", "sources": [{"kind": "meta", "name": "og:title"}]},
        {"name": "released", "type": "date", "sources": [{"kind": "css", "selector": "span.date", "extract": "text", "transforms": [{"op": "date", "day_first": True}]}]},
        {"name": "in_stock", "type": "boolean", "sources": [{"kind": "jsonld", "schema_type": "Offer", "path": "availability"}]},
        {"name": "stock_text", "type": "boolean", "sources": [{"kind": "css", "selector": "span.stock", "extract": "text"}]},
        {"name": "sku_num", "type": "number", "sources": [{"kind": "jsonld", "schema_type": "Product", "path": "sku", "transforms": [{"op": "regex_extract", "pattern": "(\\d+)"}]}]},
        {"name": "missing", "type": "text", "sources": [{"kind": "css", "selector": "h9", "extract": "text"}]},
        {"name": "missing_list", "type": "url", "multiple": True, "sources": [{"kind": "css", "selector": "h9", "extract": "text"}]},
    ],
}


def test_apply_mapping_all_types():
    ext = apply_mapping(MAPPING, page(), "auto")
    d = ext.data
    assert d["title"] == "Aurora 60"
    assert d["price"] == 1299.0
    assert d["currency"] == "EUR"
    assert d["images"] == ["https://www.lumen-shop.example/g1.jpg", "https://www.lumen-shop.example/g2.jpg"]
    assert ext.source_hits["images"] == 1
    assert d["og"] == "OG Aurora"
    assert d["released"] == "2026-11-03"
    assert d["in_stock"] is True and d["stock_text"] is True
    assert d["sku_num"] == 60.0
    assert d["missing"] is None and d["missing_list"] == []
    html = d["description"]
    assert "<p>" in html and "<ul>" in html and "<table>" in html
    for bad in ("<script", "<style", "<form", "<iframe", "onclick", "data-gtm", "data-track", "data-ga", "utm_source", "<input"):
        assert bad not in html, bad
    assert 'href="https://www.lumen-shop.example/kat?id=1"' in html
    assert 'src="https://www.lumen-shop.example/img/c.jpg"' in html
    assert ext.missing({"title", "missing"}) == ["missing"]


def test_extraction_mode_filters_sources():
    ext = apply_mapping(MAPPING, page(), "structured_data")
    assert ext.data["price"] is None and ext.data["title"] == "Aurora 60"
    ext = apply_mapping(MAPPING, page(), "selectors")
    assert ext.data["title"] is None and ext.data["og"] == "OG Aurora"


def test_template_precedence_and_match():
    a = dict(MAPPING, template_id="b_second")
    b = dict(MAPPING, template_id="a_first")
    assert pick_template([a, b], page(), BASE)["template_id"] == "a_first"
    c = dict(MAPPING, template_id="c", match={"url_patterns": ["/nope/"]})
    assert pick_template([c], page(), BASE) is None
    assert required_set(MAPPING, ["og"]) == {"title", "og"}


@pytest.mark.parametrize("text,sep,expected", [
    ("1.299,00 €", "auto", 1299.0), ("1,299.00", "auto", 1299.0), ("1.299", "auto", 1299.0), ("1,5", "auto", 1.5),
    ("12.50", "auto", 12.5), ("1 234,56", "auto", 1234.56), ("1'234.5", "auto", 1234.5), ("ab 389,- EUR", "auto", 389.0),
    ("1.234.567", "auto", 1234567.0), ("1.299,00", "comma", 1299.0), ("1,299.00", "dot", 1299.0), ("-3,5", "auto", -3.5),
    ("kein Preis", "auto", None), ("12 345", "auto", 12345.0),
])
def test_number(text, sep, expected):
    assert T.number(text, sep) == expected


@pytest.mark.parametrize("text,default,expected", [
    ("EUR 12", None, "EUR"), ("12 €", None, "EUR"), ("£5", None, "GBP"), ("$5", None, None), ("$5", "USD", "USD"),
    ("12 zł", None, "PLN"), ("100 Kč", None, "CZK"), ("9 kr", "SEK", "SEK"), ("CHF 10", None, "CHF"), ("nothing", None, None),
])
def test_currency(text, default, expected):
    assert T.currency(text, default) == expected


@pytest.mark.parametrize("text,day_first,expected", [
    ("2026-11-03", False, "2026-11-03"), ("2026-11-03T10:00:00Z", False, "2026-11-03"), ("03.11.2026", False, "2026-11-03"),
    ("11/03/2026", False, "2026-11-03"), ("03/11/2026", True, "2026-11-03"), ("31.02.2026", False, None), ("x", False, None),
])
def test_date(text, day_first, expected):
    assert T.to_date(text, day_first) == expected


def test_trim_regex_absolute():
    assert T.trim("  a \n\t b  ") == "a b"
    assert T.regex_extract("Art.-Nr. AB-12", r"Nr\.\s*([A-Z0-9-]+)") == "AB-12"
    assert T.regex_extract("abc", r"(x)") is None
    assert T.regex_extract("abc", r"b", 0) == "b"
    assert T.absolute_url("/x.jpg", "https://e.example/a/b") == "https://e.example/x.jpg"
    assert T.absolute_url("javascript:void(0)", "https://e.example/") is None
    assert T.price("€ 1.299,-") == 1299.0


@pytest.mark.parametrize("value,expected", [
    ("yes", True), ("Nein", False), ("1", True), ("off", False), ("https://schema.org/OutOfStock", False),
    ("http://schema.org/PreOrder", True), ("schema:SoldOut", False), ("maybe", None), (True, True),
])
def test_boolean(value, expected):
    assert T.to_boolean(value) == expected


def test_html_clean_keeps_structure():
    out = clean_html("<div><p>a<br>b</p><table><tr><th>x</th></tr></table><script>1</script><a href='rel' onmouseover='x'>l</a></div>", "https://e.example/d/")
    assert "<br>" in out and "<table>" in out and "<th>" in out
    assert "script" not in out and "onmouseover" not in out
    assert 'href="https://e.example/d/rel"' in out


def test_page_signature_stable_for_same_template():
    p1 = Page("<html><body><div class='a'><h1>x</h1><ul><li>1</li><li>2</li></ul></div></body></html>", "https://e.example/1")
    p2 = Page("<html><body><div class='a'><h1>y</h1><ul><li>1</li></ul></div></body></html>", "https://e.example/2")
    p3 = Page("<html><body><main class='b'><h2>y</h2></main></body></html>", "https://e.example/3")
    assert p1.signature == p2.signature != p3.signature
