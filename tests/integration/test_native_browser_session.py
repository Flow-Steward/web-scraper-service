"""Ordinary BrowserSession.get uses native per-hop policy without a substitute."""
import pytest
import pytest_asyncio
from playwright.async_api import Route

from app.fetching.destinations import DestinationDenied
from app.fetching.oneoff import BrowserSession
from tests.integration.test_redirect_destinations import redirect_documents, authenticated_robots_redirects
from tests.integration.test_firefox_native_policy import native_documents


@pytest_asyncio.fixture(params=["chromium", "camoufox"])
async def native_session(request, monkeypatch):
    from tests.unit.test_logic import settings
    monkeypatch.setattr("app.settings._settings", settings())
    async def no_geo(*args, **kwargs):
        return None
    async def context(browser, *args, **kwargs):
        return await browser.new_context(service_workers="block")
    async def forbidden(*args, **kwargs):
        raise AssertionError("BrowserSession documents must retain native transport")
    monkeypatch.setattr("app.fetching.geo.exit_geo", no_geo)
    monkeypatch.setattr("app.fetching.identity.identity_for", no_geo)
    monkeypatch.setattr("app.fetching.identity.chromium_context", context)
    monkeypatch.setattr("app.fetching.identity.camoufox_context", context)
    monkeypatch.setattr(Route, "fetch", forbidden)
    session = BrowserSession(request.param, timeout_s=10)
    try:
        await session.start()
        yield session
    finally:
        await session.close()


async def test_ordinary_browser_session_denied_redirect_zero_hits(native_session, redirect_documents):
    origin, _, hits = redirect_documents
    with pytest.raises(DestinationDenied):
        await native_session.get(origin + "/list?page=2")
    assert [path for path, _ in hits] == ["/list?page=2", "/list/next?page=3"]
    assert "/cart?add-to-cart=42" not in [path for path, _ in hits]


async def test_ordinary_browser_session_allowed_identity_and_rebind(native_session, redirect_documents):
    origin, redirects, hits = redirect_documents
    redirects["/list/next?page=3"] = "/list/final?edition=mono"
    status, html, final, _ = await native_session.get(origin + "/list?page=2")
    assert status == 200
    assert final == origin + "/list/final?edition=mono"
    assert "Allowed listing" in html
    previous = native_session._native_document_policy
    status, html, final, _ = await native_session.get(origin + "/second-list")
    assert status == 200 and final == origin + "/second-list"
    assert native_session._native_document_policy is previous
    assert "redirect_cookie=kept" in hits[2][1].get("Cookie", "")


async def test_ordinary_browser_session_caller_denial_before_redirect(native_session, redirect_documents):
    origin, redirects, hits = redirect_documents
    redirects["/list/next?page=3"] = "/list/robots-blocked"
    checked = []
    async def allowed(url):
        checked.append(url)
        return not url.endswith("robots-blocked")
    with pytest.raises(DestinationDenied):
        await native_session.get(origin + "/list?page=2", destination_policy=allowed)
    assert origin + "/list/robots-blocked" in checked
    assert "/list/robots-blocked" not in [path for path, _ in hits]


async def test_native_redirect_robots_cache_miss_has_no_worker_queue_deadlock(native_session, authenticated_robots_redirects):
    import asyncio
    from types import SimpleNamespace
    from app.jobs.runner import JobRunner

    origin, hits = authenticated_robots_redirects
    async def saturated_worker_queue(*args, **kwargs):
        raise AssertionError("Held navigation must not enqueue robots into its occupied worker")
    runner = SimpleNamespace(config={"respect_robots": True, "headers": {"X-Robots-Key": "retained"}}, robots={},
        _shutdown=False, state="running", aux_fetch=saturated_worker_queue,
        throttle=SimpleNamespace(set_crawl_delay=lambda *args: None))
    async def policy(url):
        return await JobRunner._browser_redirect_robots_allowed(runner, native_session.page, url)
    async with asyncio.timeout(3):
        with pytest.raises(DestinationDenied):
            await native_session.get(origin + "/list", destination_policy=policy)
    paths = [path for path, _ in hits]
    assert paths.count("/robots.txt") == 1
    assert next(headers for path, headers in hits if path == "/robots.txt")["X-Robots-Key"] == "retained"
    assert "/list/next" in paths
    assert "/list/final" not in paths
