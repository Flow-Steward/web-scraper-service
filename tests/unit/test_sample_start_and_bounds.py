"""Start-page admission and bounded traversal use the same cached observation."""

import json
from urllib.parse import urlsplit

import pytest

from app.core.config import merge_layers
from app.fetching.oneoff import Fetched
from app.service import Service
from app.setup_ops import SetupFetch, _samples
from tests.conftest import profile
from tests.unit.test_logic import settings


async def sample_graph(tmp_path, monkeypatch, pages, config, final_urls=None, count=1, urls=None):
    svc = Service(settings(data_dir=str(tmp_path), secrets_key="test-key-012345678901234567"))
    monkeypatch.setattr("app.settings._settings", svc.settings)
    config, _ = merge_layers({"fetch_mode": "http", "discovery_mode": "crawl", "fields": ["title"],
                              "delay_min_seconds": 0, "delay_max_seconds": 0,
                              "max_requests_per_minute": 60000, **config}, {}, {})
    requests = []

    async def public(url):
        return False

    async def fetch(url, **kwargs):
        requests.append(url)
        html = pages.get(urlsplit(url).path)
        return Fetched(url, (final_urls or {}).get(url, url), 200 if html is not None else 404, (html or "Missing").encode())

    monkeypatch.setattr("app.setup_ops.url_is_private", public)
    monkeypatch.setattr("app.setup_ops.http_get", fetch)
    sf = SetupFetch(svc, config, profile())
    try:
        answer = await _samples(svc, {"urls": urls or []}, config, count, sf)
        return answer, requests
    finally:
        await sf.close()
        svc.db.close()


@pytest.mark.parametrize("mode", ["crawl", "auto", "sitemap"])
async def test_single_page_samples_only_the_requested_product(tmp_path, monkeypatch, mode):
    origin = "https://supplier.test"
    item = '<main class="sheet"><h1>Original</h1></main><a href="/p/2">Related</a>'
    answer, requests = await sample_graph(tmp_path, monkeypatch,
        {"/p/1": item, "/p/2": '<main class="sheet"><h1>Related</h1></main>'},
        {"site_url": origin + "/p/1", "single_page": True,
         "discovery_mode": mode, "target_page_selector": ".sheet"}, count=5)
    assert [sample["url"] for sample in answer["samples"]] == [origin + "/p/1"]
    assert answer["requested_count"] == 1
    assert answer["warnings"] == []
    assert requests == [origin + "/p/1"]


async def test_single_page_does_not_guess_sibling_targets_when_start_is_not_target(tmp_path, monkeypatch):
    origin = "https://supplier.test"
    links = ''.join(f'<a href="/p/{i}">Item</a>' for i in range(3))
    pages = {"/entry": '<h1>Entry</h1>' + links,
             **{f"/p/{i}": '<h1>Sibling</h1><span>EUR 12</span>' for i in range(3)}}
    answer, requests = await sample_graph(tmp_path, monkeypatch, pages,
        {"site_url": origin + "/entry", "single_page": True}, count=5)
    assert answer["samples"] == []
    assert answer["requested_count"] == 1
    assert requests == [origin + "/entry"]


async def test_single_page_preserves_requested_spa_route_and_session_query(tmp_path, monkeypatch):
    url = "https://supplier.test/?sessionid=fixture-session#/product/1"
    answer, requests = await sample_graph(tmp_path, monkeypatch,
        {"/": '<main class="sheet"><h1>Selected product</h1></main>'},
        {"site_url": url, "single_page": True, "target_page_selector": ".sheet"})
    assert len(answer["samples"]) == 1
    assert requests == [url]


async def test_explicit_sample_urls_take_precedence_over_single_page(tmp_path, monkeypatch):
    origin = "https://supplier.test"
    urls = [origin + "/p/2", origin + "/p/3"]
    answer, requests = await sample_graph(tmp_path, monkeypatch,
        {"/p/2": '<h1>Two</h1>', "/p/3": '<h1>Three</h1>'},
        {"site_url": origin + "/p/1", "single_page": True}, count=5, urls=urls)
    assert [sample["url"] for sample in answer["samples"]] == urls
    assert answer["requested_count"] == 2
    assert requests == urls


@pytest.mark.parametrize("mode", ["sitemap", "sitemap_and_crawl"])
async def test_sitemap_sampling_follows_links_only_when_crawling_enabled(tmp_path, monkeypatch, mode):
    origin = "https://supplier.test"
    answer, requests = await sample_graph(tmp_path, monkeypatch,
        {"/sitemap.xml": '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>' + origin + '/p/1</loc></url></urlset>',
         "/p/1": '<h1>Not a target</h1><a href="/p/2">Related page</a>',
         "/p/2": '<main class="sheet"><h1>Outside the sitemap</h1></main>'},
        {"site_url": origin + "/", "sitemap_url": origin + "/sitemap.xml",
         "discovery_mode": mode, "target_page_selector": ".sheet"})
    assert origin + "/p/1" in requests
    if mode == "sitemap":
        assert answer["samples"] == []
        assert origin + "/p/2" not in requests
    else:
        assert [sample["url"] for sample in answer["samples"]] == [origin + "/p/2"]


async def test_empty_sitemap_does_not_fall_back_to_start_page_links(tmp_path, monkeypatch):
    origin = "https://supplier.test"
    answer, requests = await sample_graph(tmp_path, monkeypatch,
        {"/sitemap.xml": '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"/>',
         "/": '<a href="/p/1">Outside the sitemap</a>',
         "/p/1": '<main class="sheet"><h1>Outside the sitemap</h1></main>'},
        {"site_url": origin + "/", "sitemap_url": origin + "/sitemap.xml",
         "discovery_mode": "sitemap", "target_page_selector": ".sheet"})
    assert answer["samples"] == []
    assert origin + "/" not in requests
    assert origin + "/p/1" not in requests


async def test_product_sampling_reaches_product_maps_before_unrelated_map_exhausts_budget(tmp_path, monkeypatch):
    origin = "https://supplier.test"
    index = '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + ''.join(
        '<sitemap><loc>' + origin + path + '</loc></sitemap>'
        for path in ('/sitemap-oem-0.xml', '/sitemap-products-0.xml')) + '</sitemapindex>'
    metadata = '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + ''.join(
        f'<url><loc>{origin}/oem/{i}</loc></url>' for i in range(5000)) + '</urlset>'
    answer, requests = await sample_graph(tmp_path, monkeypatch,
        {"/sitemap.xml": index, "/sitemap-oem-0.xml": metadata,
         "/sitemap-products-0.xml": '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>' + origin + '/p/1</loc></url></urlset>',
         "/p/1": '<main class="sheet"><h1>Product</h1></main>'},
        {"site_url": origin + "/", "sitemap_url": origin + "/sitemap.xml",
         "discovery_mode": "sitemap", "structured_data_types": ["Product"],
         "url_include": [r"/p/"], "target_page_selector": ".sheet"})
    assert [sample["url"] for sample in answer["samples"]] == [origin + "/p/1"]
    assert requests.index(origin + "/sitemap-products-0.xml") < requests.index(origin + "/sitemap-oem-0.xml")


async def test_matching_start_page_is_sampled_before_nonmatching_listing_links(tmp_path, monkeypatch):
    origin = "https://supplier.test"
    original = '<h1>Original item</h1><a href="/p/2">Related</a><a href="/p/3">Related</a>'
    answer, requests = await sample_graph(tmp_path, monkeypatch,
        {"/p/1": original, "/p/2": "<h1>Related two</h1>", "/p/3": "<h1>Related three</h1>"},
        {"site_url": origin + "/p/1", "url_include": [r"/p/1$"]})
    assert [sample["url"] for sample in answer["samples"]] == [origin + "/p/1"]
    assert "Original item" in answer["samples"][0]["distilled_html"]
    assert requests == [origin + "/p/1"]  # discovery and extraction share the fetched body


@pytest.mark.parametrize("target_hint", ["structured", "selector"])
async def test_fresh_target_start_is_sampled_without_url_patterns(tmp_path, monkeypatch, target_hint):
    origin = "https://supplier.test"
    content = '<main class="sheet"><h1>Original item</h1></main><nav><a href="/contact">Contact</a></nav>'
    config = {"site_url": origin + "/item"}
    if target_hint == "structured":
        content += '<script type="application/ld+json">' + json.dumps({"@type": "Product", "name": "Original item"}) + '</script>'
    else:
        config["target_page_selector"] = ".sheet"
    answer, requests = await sample_graph(tmp_path, monkeypatch,
        {"/item": content, "/contact": "<h1>Contact</h1>"}, config)
    assert [sample["url"] for sample in answer["samples"]] == [origin + "/item"]
    assert "Original item" in answer["samples"][0]["distilled_html"]
    assert requests == [origin + "/item"]
    start = answer["explanation"]["discovery"]["start_page"]
    assert start["hrefs"] == start["in_site"] == 1
    assert start["shapes"] == [{"shape": "/contact", "links": 1, "in_menus": 1.0}]


@pytest.mark.parametrize("single_page", [False, True])
@pytest.mark.parametrize("policy", [{"target_page_selector": ".missing"}, {"url_include": [r"/other$"]}, {"target_page_selector": ".sheet", "url_exclude": [r"/item$"]}, {"url_include": [r"/item$"], "url_exclude": [r"/item$"]}])
async def test_start_target_evidence_does_not_override_explicit_admission_policy(tmp_path, monkeypatch, policy, single_page):
    origin = "https://supplier.test"
    content = '<main class="sheet"><h1>Item</h1></main><a href="/contact">Contact</a><script type="application/ld+json">' + json.dumps({"@type": "Product", "name": "Item"}) + '</script>'
    answer, requests = await sample_graph(tmp_path, monkeypatch,
        {"/item": content, "/contact": "<h1>Contact</h1>"}, {"site_url": origin + "/item", "single_page": single_page, **policy})
    assert answer["samples"] == []
    assert all(page["url"] != origin + "/item" for page in answer["explanation"]["pages"]
               if page["outcome"] not in (("start", "not_target") if single_page else ("start",)))
    assert requests.count(origin + "/item") == (0 if single_page and policy.get("url_exclude") else 1)


@pytest.mark.parametrize("single_page", [False, True])
async def test_redirected_start_uses_loaded_final_url_for_target_admission(tmp_path, monkeypatch, single_page):
    origin = "https://supplier.test"
    requested, final = origin + "/entry", origin + "/item"
    content = '<main class="sheet"><h1>Original item</h1></main><a href="/contact">Contact</a>'
    answer, requests = await sample_graph(tmp_path, monkeypatch,
        {"/entry": content, "/contact": "<h1>Contact</h1>"},
        {"site_url": requested, "single_page": single_page, "target_page_selector": ".sheet", "url_include": [r"/item$"]},
        final_urls={requested: final})
    assert [sample["url"] for sample in answer["samples"]] == [final]
    assert requests == [requested]


async def test_start_candidate_shares_the_existing_product_url_shape(tmp_path, monkeypatch):
    origin = "https://supplier.test"
    content = '<main class="sheet"><h1>Original</h1></main><a href="/p/2">Related</a>'
    answer, requests = await sample_graph(tmp_path, monkeypatch,
        {"/p/1": content, "/p/2": '<main class="sheet"><h1>Related</h1></main>'},
        {"site_url": origin + "/p/1", "target_page_selector": ".sheet"})
    assert [sample["url"] for sample in answer["samples"]] == [origin + "/p/1"]
    assert answer["explanation"]["discovery"]["shapes"] == 1
    assert requests == [origin + "/p/1"]


async def test_backlinks_do_not_reprobe_the_already_observed_start_page(tmp_path, monkeypatch):
    origin = "https://supplier.test"
    item = '<script type="application/ld+json">' + json.dumps({"@type": "Product", "name": "Item"}) + '</script><h1>Item</h1><a href="/catalogue">Back</a><a href="/details/deeper">Details</a>'
    answer, requests = await sample_graph(tmp_path, monkeypatch,
        {"/catalogue": '<a href="/p/1">One</a><a href="/p/2">Two</a>',
         "/p/1": item, "/p/2": item, "/details/deeper": '<h1>Details</h1><a href="/catalogue">Back</a>'},
        {"site_url": origin + "/catalogue", "target_page_selector": ".missing"})
    attempts = [page for page in answer["explanation"]["pages"] if page["outcome"] != "start"]
    assert all(page["url"] != origin + "/catalogue" for page in attempts)
    assert len({page["url"] for page in attempts}) == len(attempts)
    assert answer["samples"] == []
    assert any(page["url"] == origin + "/details/deeper" for page in attempts)
    assert len(attempts) <= 20


async def test_missing_selector_does_not_follow_an_endless_chain_past_depth_budget(tmp_path, monkeypatch):
    origin = "https://supplier.test"
    pages = {"/catalogue": '<a href="/node/0">Enter</a>'}
    pages.update({f"/node/{i}": f'<h1>Intermediate</h1><a href="/node/{i + 1}">Deeper</a>' for i in range(40)})
    answer, requests = await sample_graph(tmp_path, monkeypatch, pages,
        {"site_url": origin + "/catalogue", "target_page_selector": ".missing"})
    attempts = [page for page in answer["explanation"]["pages"] if page["outcome"] != "start"]
    assert answer["samples"] == []
    assert [page["url"] for page in attempts] == [origin + f"/node/{i}" for i in range(4)]
    assert all(page["reason"] == "target_page_selector absent" for page in attempts)
    assert requests == [origin + "/catalogue", *(page["url"] for page in attempts)]
