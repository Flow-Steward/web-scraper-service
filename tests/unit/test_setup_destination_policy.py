"""Setup uses the same destination policy for redirects and listing discovery."""

from types import SimpleNamespace

import pytest

from app.core.config import merge_layers
from app.fetching.oneoff import Fetched
from app.service import Service
from app.setup_ops import SetupFetch
from tests.unit.test_logic import settings

SITE = "https://supplier.test/catalogue/"


@pytest.fixture
def setup_fetch(tmp_path, monkeypatch):
    svc = Service(settings(data_dir=str(tmp_path), secrets_key="test-key-012345678901234567"))
    monkeypatch.setattr("app.settings._settings", svc.settings)
    monkeypatch.setattr("app.fetching.browsers.resolve_browser", lambda _: None)
    config, _ = merge_layers({"site_url": SITE, "crawl_include": [r"/catalogue(?:/|$)"], "url_include": [r"/product/"], "url_exclude": [r"/excluded(?:/|$)"], "delay_min_seconds": 0, "delay_max_seconds": 0}, {}, {})
    sf = SetupFetch(svc, config, {"proxy_tiers": []})
    sf._navigation_robots["https://supplier.test"] = SimpleNamespace(is_allowed=lambda url: "/blocked/" not in url, get_crawl_delay=lambda: None)
    yield sf
    svc.db.close()


@pytest.mark.parametrize("url,allowed", [
    (SITE + "next/", True),
    ("https://supplier.test/product/item", True),
    ("https://supplier.test/product/item?color=blue", True),
    ("https://supplier.test/unrelated/", False),
    (SITE + "excluded/", False),
    (SITE + "blocked/", False),
    ("https://other.test/catalogue/", False),
    ("https://user:password@supplier.test/catalogue/", False),
    ("https://supplier.test/cart/", False),
    (SITE + "?add-to-cart=1", False),
])
async def test_setup_document_destination_policy(setup_fetch, url, allowed):
    assert await setup_fetch._destination_allowed(url) is allowed


async def test_setup_auxiliary_policy_keeps_file_and_scope_exemptions(setup_fetch):
    assert await setup_fetch._aux_destination_allowed("https://supplier.test/sitemaps/products.xml")
    assert not await setup_fetch._aux_destination_allowed("https://other.test/sitemap.xml")
    assert not await setup_fetch._aux_destination_allowed("https://supplier.test/cart/")


@pytest.mark.parametrize("url,allowed", [(SITE, False), ("https://supplier.test/sitemap.xml", True)])
async def test_setup_http_redirect_uses_document_or_auxiliary_policy(setup_fetch, monkeypatch, url, allowed):
    result = []

    async def http(original, *, destination_policy=None, **kwargs):
        assert destination_policy is not None
        result.append(await destination_policy("https://supplier.test/sitemaps/products.xml"))
        return Fetched(original, original, 200, b"<main>ok</main>")

    monkeypatch.setattr("app.setup_ops.http_get", http)
    await setup_fetch.http(url)
    assert result == [allowed]


async def test_setup_browser_documents_pass_policy_to_native_guard(setup_fetch):
    async def get(url, *, destination_policy=None, **kwargs):
        assert destination_policy is not None
        assert not await destination_policy("https://other.test/catalogue/")
        assert not await destination_policy(SITE + "blocked/")
        assert await destination_policy("https://supplier.test/product/item?color=blue")
        return 200, "<main>ok</main>", url, None

    async def session():
        return SimpleNamespace(get=get, is_closed=False)

    setup_fetch.session = session
    assert (await setup_fetch.browser_get(SITE))[0] == 200


async def test_setup_browser_json_uses_guarded_raw_fetch(setup_fetch, monkeypatch):
    async def public(url):
        return False

    async def raw(url, *, destination_policy=None, headers=None):
        assert destination_policy is not None
        assert not await destination_policy("https://other.test/products/item.json")
        assert headers == {"Accept": "application/json"}
        return Fetched(url, url, 200, b'{"id": 1}')

    monkeypatch.setattr("app.setup_ops.url_is_private", public)
    monkeypatch.setattr(setup_fetch, "needs_browser", lambda url: True)
    setup_fetch._session = SimpleNamespace(raw=raw, is_closed=False)
    assert await setup_fetch.fetch_json("https://supplier.test/products/item.json") == {"id": 1}


async def test_setup_http_json_uses_auxiliary_redirect_policy(setup_fetch, monkeypatch):
    async def public(url):
        return False

    async def http(url, *, destination_policy=None, **kwargs):
        assert destination_policy is not None
        assert not await destination_policy("https://supplier.test/cart/")
        assert await destination_policy("https://supplier.test/data/item.json")
        return Fetched(url, url, 200, b'{"id": 2}')

    monkeypatch.setattr("app.setup_ops.url_is_private", public)
    monkeypatch.setattr("app.setup_ops.http_get", http)
    assert await setup_fetch.fetch_json("https://supplier.test/products/item.json") == {"id": 2}


async def test_setup_document_redirect_loads_and_caches_robots(setup_fetch, monkeypatch):
    setup_fetch._navigation_robots.clear()
    calls = []

    async def http(url, *, destination_policy=None, **kwargs):
        calls.append(url)
        assert destination_policy is not None
        return Fetched(url, url, 200, b"User-agent: *\nDisallow: /catalogue/blocked/\n")

    monkeypatch.setattr("app.setup_ops.http_get", http)
    assert not await setup_fetch._destination_allowed(SITE + "blocked/")
    assert await setup_fetch._destination_allowed(SITE + "next/")
    assert calls == ["https://supplier.test/robots.txt"]


@pytest.mark.parametrize("url", ["https://second.test/catalogue/item", "https://second.test/sitemap.xml"])
async def test_setup_redirect_policy_binds_each_admitted_host(setup_fetch, monkeypatch, url):
    setup_fetch.config["respect_robots"] = False
    async def http(original, *, destination_policy=None, **kwargs):
        assert destination_policy is not None
        assert await destination_policy(original + "?page=2")
        assert not await destination_policy("https://supplier.test/catalogue/other")
        return Fetched(original, original, 200, b"ok")
    monkeypatch.setattr("app.setup_ops.http_get", http)
    await setup_fetch.http(url)


async def test_setup_browser_redirect_policy_binds_each_admitted_host(setup_fetch):
    setup_fetch.config["respect_robots"] = False
    initial = "https://second.test/catalogue/item"
    async def get(url, *, destination_policy=None, **kwargs):
        assert destination_policy is not None
        assert await destination_policy(url + "?page=2")
        assert not await destination_policy(SITE + "other")
        return 200, "<main>ok</main>", url, None
    async def session():
        return SimpleNamespace(get=get, is_closed=False)
    setup_fetch.session = session
    assert (await setup_fetch.browser_get(initial))[2] == initial
