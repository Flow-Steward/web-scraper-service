"""A root/catalogue/category/item graph is walked without a manual item list."""

import pytest

from app.core.config import merge_layers
from app.extraction.page import Page
from app.fetching.oneoff import Fetched
from app.service import Service
from app.setup_ops import SetupFetch, _samples
from tests.conftest import profile
from tests.unit.test_logic import settings


@pytest.mark.parametrize("mode", ["http", "auto"])
async def test_samples_follow_multiple_listing_levels_until_targets(
    tmp_path, monkeypatch, mode
):
    svc = Service(
        settings(data_dir=str(tmp_path), secrets_key="test-key-012345678901234567")
    )
    monkeypatch.setattr("app.settings._settings", svc.settings)
    origin = "https://supplier.test"
    config, _ = merge_layers(
        {
            "site_url": origin + "/",
            "discovery_mode": "crawl",
            "fetch_mode": mode,
            "target_page_selector": ".sheet",
            "fields": ["title"],
            "url_include": ["/items/"],
        },
        {},
        {},
    )
    pages = {
        "/": '<main><a href="/catalogue">Catalogue</a></main>',
        "/catalogue": '<main><a href="/makers/a">Maker A</a><a href="/makers/b">Maker B</a></main>',
        "/makers/a": '<main><a href="/items/a?choice=one">A</a></main>',
        "/makers/b": '<main><a href="/items/b?choice=two">B</a></main>',
        "/items/a?choice=one": '<main class="sheet"><h1>One</h1><script>window.ready=true;</script></main>',
        "/items/b?choice=two": '<main class="sheet"><h1>Two</h1><script>window.ready=true;</script></main>',
    }
    loaded = []
    browser_calls = []

    async def browser_unneeded(self, url, **kwargs):
        browser_calls.append(url)
        raise AssertionError("already usable target should stay on HTTP")

    async def fetch(self, url):
        return Fetched(url, url, 200, pages[url.removeprefix(origin)].encode())

    async def fetch_page(_svc, url, _config, sf):
        loaded.append(url)
        sf.last_how = "http"
        return Page(pages[url.removeprefix(origin)], url, 200), {"mode": "http"}, None

    async def public(_url):
        return False

    monkeypatch.setattr(SetupFetch, "fetch", fetch)
    monkeypatch.setattr(SetupFetch, "browser_get", browser_unneeded)
    monkeypatch.setattr("app.setup_ops.fetch_page", fetch_page)
    monkeypatch.setattr("app.setup_ops.url_is_private", public)
    sf = SetupFetch(svc, config, profile())
    try:
        result = await _samples(svc, {"config": config, "urls": []}, config, 2, sf)
        assert {sample["url"] for sample in result["samples"]} == {
            origin + "/items/a?choice=one",
            origin + "/items/b?choice=two",
        }
        assert browser_calls == []
        assert len(loaded) <= 10
        assert all(url.startswith(origin) for url in loaded)
    finally:
        await sf.close()
        svc.db.close()


async def test_selector_only_mapping_keeps_usable_scripted_target_on_http(tmp_path, monkeypatch):
    from app.extraction.mapping import pick_template

    svc = Service(
        settings(data_dir=str(tmp_path), secrets_key="test-key-012345678901234567")
    )
    monkeypatch.setattr("app.settings._settings", svc.settings)
    origin = "https://supplier.test"
    config, _ = merge_layers(
        {"site_url": origin + "/item", "fetch_mode": "auto", "fields": ["title"]},
        {},
        {},
    )
    mapping = {
        "schema_version": 1,
        "template_id": "selector_sheet",
        "match": {"selector": ".sheet"},
        "fields": [
            {
                "name": "title",
                "type": "text",
                "required": True,
                "sources": [{"kind": "css", "selector": "h1", "extract": "text"}],
            }
        ],
    }
    with svc.db.tx():
        svc.mappings.activate("supplier.test", "selector_sheet", 0, mapping, None)
    page = Page(
        '<main class="sheet"><h1>Usable item</h1><p>'
        + "Complete server-rendered product description. " * 30
        + '</p></main><script src="/assets/site.js"></script>',
        origin + "/item",
    )
    sf = SetupFetch(svc, config, profile())
    try:
        assert page.visible_text_chars > 300
        assert pick_template([mapping], page, page.url) is mapping
        assert sf.needs_browser_navigation(page) is False
    finally:
        await sf.close()
        svc.db.close()


async def test_samples_follow_policy_validated_deferred_pagination(tmp_path, monkeypatch):
    svc = Service(
        settings(data_dir=str(tmp_path), secrets_key="test-key-012345678901234567")
    )
    monkeypatch.setattr("app.settings._settings", svc.settings)
    origin = "https://supplier.test"
    config, _ = merge_layers(
        {
            "site_url": origin + "/list",
            "discovery_mode": "crawl",
            "fetch_mode": "http",
            "target_page_selector": ".sheet",
            "fields": ["title"],
            "url_include": ["/items/"],
        }, {}, {},
    )
    pages = {
        "/list": '<main><button>Next</button></main>',
        "/list?page=2": '<main><a href="/items/a">Item</a></main>',
        "/items/a": '<main class="sheet"><h1>Item</h1></main>',
    }
    loaded = []

    async def fetch(self, url):
        return Fetched(url, url, 200, pages[url.removeprefix(origin)].encode())

    async def fetch_page(_svc, url, _config, sf):
        loaded.append(url)
        sf.last_how = "http"
        return Page(pages[url.removeprefix(origin)], url, 200), {"mode": "http"}, None

    async def deferred_navigation(self, url, page=None):
        if page is not None and url == origin + "/list":
            # The browser request guard validated and deferred this destination.
            page.extra_links = [origin + "/list?page=2"]
            page.deferred_links = [origin + "/list?page=2"]

    async def public(_url):
        return False

    monkeypatch.setattr(SetupFetch, "fetch", fetch)
    monkeypatch.setattr(SetupFetch, "discover_links", deferred_navigation)
    monkeypatch.setattr("app.setup_ops.fetch_page", fetch_page)
    monkeypatch.setattr("app.setup_ops.url_is_private", public)
    sf = SetupFetch(svc, config, profile())
    try:
        result = await _samples(svc, {"config": config, "urls": []}, config, 1, sf)
        assert [sample["url"] for sample in result["samples"]] == [origin + "/items/a"]
        assert loaded == [origin + "/list", origin + "/list?page=2", origin + "/items/a"]
    finally:
        await sf.close()
        svc.db.close()
