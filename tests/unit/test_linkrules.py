"""Link rules (README 4.11), canonical helpers and session parameters."""

from __future__ import annotations

from app.core.linkrules import RULES, DynamicParams, enabled_rules, key_url, offsite_rule, raw_skip, trap_pattern, url_skip
from app.core.urls import canonical_url
from app.jobs.canon import identity, link_header_canonical, only_pagination_differs, rekey

ALL = set(RULES)
S = "https://shop.example"


def test_sensitive_segments_are_whole_segments():
    for path in ("/customer/account/login/", "/checkout/cart/", "/wishlist/index/add/product/1/", "/catalog/product_compare/add/",
                 "/catalogsearch/result/", "/review/product/list/id/3/", "/newsletter/subscriber/new/", "/sendfriend/product/send/",
                 "/my-account/", "/login.php", "/wishlist-add/5", "/account", "/print/"):
        assert url_skip(S + path, ALL) == "sensitive_path", path
    for path in ("/authentic-lamps/", "/author/jane/", "/userguide-lamp", "/cartridge-ink/", "/printer-paper/", "/accounting-software/",
                 "/terms-lamp-x", "/product/search-light-500/"):
        assert url_skip(S + path, ALL) is None, path


def test_schemes_offsite_extensions_actions_facets():
    for href in ("mailto:a@b.c", "tel:+1", "javascript:void(0)", "whatsapp://send?text=x", "viber://chat", "tg://resolve?domain=x",
                 "sms:+1", "skype:x?call", "callto:x", "facetime:x", "geo:1,2", "maps:q", "intent://x", "market://details",
                 "itms-apps://x", "fb-messenger://x", "sip:x@y", "webcal://x", "data:text/html,x", "blob:x", "ftp://x/y"):
        assert raw_skip(href, ALL) == "scheme", href
    assert raw_skip("https://shop.example/x", ALL) is None and raw_skip("/x", ALL) is None
    for u in ("https://wa.me/123", "https://api.whatsapp.com/send", "https://t.me/x", "https://m.me/x", "https://invite.viber.com/?g=x",
              "https://www.facebook.com/sharer/sharer.php?u=x", "https://twitter.com/intent/tweet", "https://pinterest.com/pin/create/button/"):
        assert offsite_rule(u, ALL) == "messenger_social", u
    assert offsite_rule("https://www.facebook.com/shop", ALL) is None
    assert url_skip(S + "/files/manual.PDF", ALL) == "file_extension" and url_skip(S + "/img/a.webp", ALL) == "file_extension"
    assert url_skip(S + "/product/lamp.html", ALL) is None and url_skip(S + "/v1.2/", ALL) is None
    assert url_skip(S + "/p/1?add-to-cart=5", ALL) == "action_params" and url_skip(S + "/x?form_key=a", ALL) == "action_params"
    for q in ("color=red", "size=xl&color=red,blue", "orderby=price", "filter_color=red", "pa_size=m", "product_list_order=name", "min_price=5"):
        assert url_skip(S + "/category/lamps/?" + q, ALL) == "facet_params", q
    assert url_skip(S + "/category/lamps/?page=2", ALL) is None and url_skip(S + "/product.php?id=5", ALL) is None


def test_rules_can_be_switched_off():
    on = enabled_rules({"link_rules_off": ["facet_params", "sensitive_path"]})
    assert "facet_params" not in on and "nofollow" in on
    assert url_skip(S + "/c/?color=red", on) is None and url_skip(S + "/login/", on) is None
    assert url_skip(S + "/a.pdf", on) == "file_extension"


def test_session_parameters_leave_the_canonical_url():
    assert canonical_url(S + "/p/?SID=abc123&id=4") == S + "/p/?id=4"
    assert canonical_url(S + "/p/?PHPSESSID=x&a=1&form_key=k&uenc=u&___store=en&___from_store=de") == S + "/p/?a=1"
    assert canonical_url(S + "/p;jsessionid=ABCDEF0123/x.html?q=1") == S + "/p/x.html?q=1"
    assert canonical_url(S + "/p/x.html;JSESSIONID=ABC") == S + "/p/x.html"


def test_key_url_and_trap_pattern():
    address = (
        S
        + "/wishlist/index/add/uenc/aHR0cHM6Ly9zaG9wLmV4YW1wbGUvcDEuaHRtbA,,/form_key/x/"
    )
    # Opaque path data is identity, not proof of an alias.
    assert key_url(address) == address
    # Action-path protection remains separate.
    assert url_skip(address, ALL) == "sensitive_path"
    assert (
        key_url(S + "/product/very-long-product-name-with-many-words-2024/")
        == S + "/product/very-long-product-name-with-many-words-2024/"
    )
    assert key_url(S + "/a?tok=1&b=2", {"tok"}) == S + "/a?b=2"
    assert trap_pattern(S + "/events?month=2026-01") == "/events?month"
    assert trap_pattern(S + "/lights/sub/color-red/size-xl/") == "/<path-filters>" == trap_pattern(S + "/decor/f/12-40/")
    assert trap_pattern(S + "/lights/sub/pendant-lamps/") == "/lights/*" == trap_pattern(S + "/lights/colorful-lamps/")
    assert trap_pattern(S + "/printers/color-laser/") == "/<path-filters>"  # shares a budget only; never skipped by itself
    assert trap_pattern(S + "/lights") == "/lights" and trap_pattern(S + "/") == "/"


def test_dynamic_parameters_are_learned_only_for_random_site_wide_values():
    d = DynamicParams(min_paths=10)
    learned = None
    for i in range(40):
        learned = d.observe(f"{S}/p{i % 12}/?tok=a1b2c3d4e5f6{i:04d}x") or learned
    assert learned == "tok" and "tok" in d.learned
    d2 = DynamicParams(min_paths=10)
    for i in range(200):
        assert d2.observe(f"{S}/product.php?id={i}") is None  # one path: an id, not a session
    d3 = DynamicParams(min_paths=10)
    for i in range(200):
        assert d3.observe(f"{S}/c{i % 30}/?page={i % 7}") is None  # pagination never


def test_canonical_helpers():
    assert link_header_canonical('<https://s/p1>; rel="canonical"', "https://s/x") == "https://s/p1"
    assert link_header_canonical('<https://s/de>; rel="alternate", </p2>; rel=canonical', "https://s/x") == "https://s/p2"
    assert link_header_canonical('<https://s/n>; rel="next"', "https://s/x") is None
    assert only_pagination_differs(S + "/c/?page=2", S + "/c/") and only_pagination_differs(S + "/c/page/3/", S + "/c/")
    assert not only_pagination_differs(S + "/c/?color=red", S + "/c/")
    assert identity({"sku": "A-1", "title": "x"}) == "sku:a-1" and identity({"title": " Lamp "}) == "title:lamp" and identity({}) is None
    rec = {"record_id": "rec_x", "source_url": S + "/a/p1.html", "template_id": "t", "item_index": 0, "data": {}}
    out = rekey(rec, S + "/p1.html", S + "/a/p1.html")
    assert out["source_url"] == S + "/p1.html" and out["requested_url"] == S + "/a/p1.html" and out["record_id"] != "rec_x"
    assert "requested_url" not in rekey(out, S + "/a/p1.html", S + "/a/p1.html")
