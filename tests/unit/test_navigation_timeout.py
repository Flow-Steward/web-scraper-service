"""Selected page timeouts bound listing navigation without a hidden lower cap."""

from types import SimpleNamespace

import pytest

from app.extraction.page import Page
from app.fetching.navigation import NavigationResult
from app.jobs.runner import JobRunner
from app.setup_ops import SetupFetch
from tests.unit.test_logic import settings

URL = "https://catalogue.example.test/list"


@pytest.mark.parametrize("seconds", [120, 15])
async def test_setup_listing_navigation_honors_selected_page_timeout(seconds):
    captured = {}

    async def collect(**kwargs):
        captured.update(kwargs)
        return NavigationResult(links=[URL + "?page=2"])

    sf = SetupFetch(
        SimpleNamespace(settings=settings(), samples=None),
        {
            "site_url": URL,
            "browser": "chromium",
            "page_timeout_seconds": seconds,
            "max_pages": 200,
        },
        {},
    )
    sf._session = SimpleNamespace(page=SimpleNamespace(url=URL), discover_links=collect, is_closed=False)
    sf._browser[URL] = (200, "<main>Listing</main>", URL, None)

    result = await sf.discover_links(URL)

    assert captured["limits"].max_seconds == seconds
    assert captured["limits"].max_steps == 100
    assert result.links == [URL + "?page=2"]


@pytest.mark.parametrize("seconds", [120, 15])
async def test_runtime_listing_navigation_honors_selected_page_timeout(
    monkeypatch, seconds
):
    captured = {}

    async def collect(browser_page, **kwargs):
        captured.update(kwargs)
        return NavigationResult(links=[URL + "?page=2"])

    monkeypatch.setattr("app.fetching.navigation.collect_listing_links", collect)
    runner = SimpleNamespace(
        config={"site_url": URL, "page_timeout_seconds": seconds, "max_pages": 200},
        _crawl_scope=lambda: [],
    )
    page = Page("<main>Listing</main>", URL)
    out = {}

    await JobRunner._collect_navigation(
        runner, SimpleNamespace(page=object()), page, URL, True, {"src": "start"}, out
    )

    assert captured["limits"].max_seconds == seconds
    assert captured["limits"].max_steps == 100
    assert page.extra_links == [URL + "?page=2"]
    assert out["navigation"]["bounded"] is False
