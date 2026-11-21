"""Document destination checks reuse policy without charging another click."""
from types import SimpleNamespace
import re

from app.extraction.page import Page
from app.fetching.navigation import NavigationAction, NavigationResult
from app.jobs.runner import JobRunner
from app.setup_ops import SetupFetch
from tests.unit.test_logic import settings

URL = "https://catalogue.example.test/list"


async def test_setup_document_policy_retains_scope_robots_without_another_action(monkeypatch):
    waits = []

    async def wait(host):
        waits.append(host)

    async def robots(url, http):
        return SimpleNamespace(
            get_crawl_delay=lambda: None,
            is_allowed=lambda target: "blocked" not in target,
        )

    monkeypatch.setattr("app.setup_ops._robots", robots)
    sf = SetupFetch(
        SimpleNamespace(settings=settings(), samples=None),
        {"site_url": URL, "browser": "chromium", "max_pages": 1,
         "crawl_include": [r"/list(?:\?|$)"], "respect_robots": True}, {},
    )
    sf._navigation_throttle.wait = wait

    async def collect(**kwargs):
        allowed = kwargs["before_action"]
        assert await allowed(NavigationAction("click", "Next page", URL))
        assert sf._navigation_actions == 1
        assert await allowed(NavigationAction("navigation", "Deferred", URL + "?page=2"))
        assert not await allowed(NavigationAction("navigation", "Deferred", URL + "?blocked=1"))
        assert not await allowed(NavigationAction("navigation", "Deferred", "https://catalogue.example.test/other"))
        return NavigationResult()

    sf._session = SimpleNamespace(page=SimpleNamespace(url=URL), discover_links=collect, is_closed=False)
    sf._browser[URL] = (200, "<main>Listing</main>", URL, None)
    await sf.discover_links(URL)
    assert sf._navigation_actions == 1
    assert waits == ["catalogue.example.test"]


async def test_runtime_document_policy_retains_scope_robots_without_another_action(monkeypatch):
    waits = []

    async def wait(host):
        waits.append(host)

    async def public(url):
        return False

    async def robots(url):
        return "blocked" not in url

    runner = SimpleNamespace(
        config={"site_url": URL, "page_timeout_seconds": 1, "max_pages": 1,
                "max_duration_minutes": 1},
        _crawl_scope=lambda: [re.compile(r"/list(?:\?|$)")],
        pages_started=0, rt={}, state="running", running_seconds=lambda: 0,
        link_rules=set(), robots_allowed=robots, paused_until=0,
        throttle=SimpleNamespace(wait=wait),
    )

    async def collect(page, **kwargs):
        allowed = kwargs["before_action"]
        click = NavigationAction("click", "Next page", URL)
        assert await allowed(click)
        await kwargs["pace_action"](click)
        assert runner.rt["navigation_actions"] == 1
        assert await allowed(NavigationAction("navigation", "Deferred", URL + "?page=2"))
        assert not await allowed(NavigationAction("navigation", "Deferred", URL + "?blocked=1"))
        assert not await allowed(NavigationAction("navigation", "Deferred", "https://catalogue.example.test/other"))
        runner.rt["stop_reason"] = "cancelled"
        assert not await allowed(NavigationAction("navigation", "Deferred", URL + "?page=3"))
        return NavigationResult()

    monkeypatch.setattr("app.jobs.runner.url_is_private", public)
    monkeypatch.setattr("app.fetching.navigation.collect_listing_links", collect)
    await JobRunner._collect_navigation(
        runner, SimpleNamespace(page=object()), Page("<main>Listing</main>", URL),
        URL, True, {"src": "start"}, {},
    )
    assert runner.rt["navigation_actions"] == 1
    assert waits == ["catalogue.example.test"]


async def test_collector_waits_for_pending_destination_policy():
    import asyncio
    from app.fetching.navigation import NavigationLimits, collect_listing_links

    class BrowserPage:
        url = URL
        main_frame = object()
        clicked = False

        async def route(self, pattern, handler):
            self.handler = handler

        async def unroute(self, pattern, handler):
            self.handler = None

        async def evaluate(self, script):
            return {"url": URL, "links": [], "anchors": [], "scrollable": False,
                    "controls": [] if self.clicked else [{"index": 0, "label": "Next page", "href": None, "nofollow": False}]}

        def locator(self, selector):
            return self

        def nth(self, index):
            return self

        async def element_handle(self, **kwargs):
            page = self

            class Element:
                async def evaluate(self, script, expected):
                    return True

                async def click(self, **kwargs):
                    page.clicked = True
                    request = SimpleNamespace(
                        method="GET", url=URL + "?page=2", frame=page.main_frame,
                        is_navigation_request=lambda: True,
                    )

                    async def fulfill(**kwargs):
                        pass

                    asyncio.create_task(page.handler(SimpleNamespace(request=request, fulfill=fulfill)))
                    await asyncio.sleep(0)

                async def dispose(self):
                    pass

            return Element()

    async def policy(action):
        if action.kind == "navigation":
            await asyncio.sleep(0.1)
        return True

    result = await collect_listing_links(
        BrowserPage(), before_action=policy,
        limits=NavigationLimits(settle_ms=300),
    )
    assert result.stop_reason == "navigation_deferred"
    assert result.deferred_links == [URL + "?page=2"]
