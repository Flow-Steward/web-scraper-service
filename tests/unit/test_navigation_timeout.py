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
    assert captured["limits"].progress_timeout is False
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
        state="running",
        config={"site_url": URL, "page_timeout_seconds": seconds, "max_pages": 200,
                "max_duration_minutes": 10},
        _crawl_scope=lambda: [],
        pages_started=5, rt={"navigation_actions": 7}, running_seconds=lambda: 15,
        last_progress=0,
    )
    page = Page("<main>Listing</main>", URL)
    out = {}

    await JobRunner._collect_navigation(
        runner, SimpleNamespace(page=object()), page, URL, True, {"src": "start"}, out
    )

    assert captured["limits"].max_seconds == seconds
    assert captured["limits"].max_steps == 188
    assert captured["limits"].max_total_seconds == 585
    assert captured["limits"].progress_timeout is True
    captured["on_progress"]()
    assert runner.last_progress > 0
    assert page.extra_links == [URL + "?page=2"]
    assert out["navigation"]["bounded"] is False


@pytest.mark.parametrize(("mode", "resolved_mode", "walks_links"), [
    ("crawl", None, True), ("auto", None, True), ("auto", "sitemap", True),
    ("url_list", None, False), ("sitemap", None, False),
])
async def test_browser_handler_does_not_truncate_a_productive_catalogue(tmp_path, monkeypatch, mode, resolved_mode, walks_links):
    from tests.unit.test_proxy_auth_pause import make_run

    svc, runner = await make_run(tmp_path, monkeypatch, config_overrides={
        "discovery_mode": mode, "fetch_mode": "browser", "max_duration_minutes": 10,
        "site_url": URL,
    })
    runner.browser = "chromium"
    if resolved_mode is not None:
        runner.doc["resolved"]["discovery_mode"] = resolved_mode
    await runner._open_storages()
    captured = {}

    class Built(Exception):
        pass

    def crawler(**kwargs):
        captured.update(kwargs)
        raise Built

    monkeypatch.setattr("crawlee.crawlers.AdaptivePlaywrightCrawler.with_parsel_static_parser", crawler)
    try:
        with pytest.raises(Built):
            runner._build_crawler()
        assert (captured["request_handler_timeout"].total_seconds() >= 10 * 60) is walks_links
    finally:
        svc.db.close()
