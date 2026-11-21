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


async def sample_graph(tmp_path, monkeypatch, pages, config, final_urls=None):
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
        answer = await _samples(svc, {"urls": []}, config, 1, sf)
        return answer, requests
    finally:
        await sf.close()
        svc.db.close()


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


@pytest.mark.parametrize("policy", [{"target_page_selector": ".missing"}, {"url_include": [r"/other$"]}, {"target_page_selector": ".sheet", "url_exclude": [r"/item$"]}, {"url_include": [r"/item$"], "url_exclude": [r"/item$"]}])
async def test_start_target_evidence_does_not_override_explicit_admission_policy(tmp_path, monkeypatch, policy):
    origin = "https://supplier.test"
    content = '<main class="sheet"><h1>Item</h1></main><a href="/contact">Contact</a><script type="application/ld+json">' + json.dumps({"@type": "Product", "name": "Item"}) + '</script>'
    answer, requests = await sample_graph(tmp_path, monkeypatch,
        {"/item": content, "/contact": "<h1>Contact</h1>"}, {"site_url": origin + "/item", **policy})
    assert answer["samples"] == []
    assert all(page["url"] != origin + "/item" for page in answer["explanation"]["pages"] if page["outcome"] != "start")
    assert requests.count(origin + "/item") == 1


async def test_redirected_start_uses_loaded_final_url_for_target_admission(tmp_path, monkeypatch):
    origin = "https://supplier.test"
    requested, final = origin + "/entry", origin + "/item"
    content = '<main class="sheet"><h1>Original item</h1></main><a href="/contact">Contact</a>'
    answer, requests = await sample_graph(tmp_path, monkeypatch,
        {"/entry": content, "/contact": "<h1>Contact</h1>"},
        {"site_url": requested, "target_page_selector": ".sheet", "url_include": [r"/item$"]},
        final_urls={requested: final})
    assert [sample["url"] for sample in answer["samples"]] == [final]
    assert requests == [requested]


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
