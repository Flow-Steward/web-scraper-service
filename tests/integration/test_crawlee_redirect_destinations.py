"""Pinned Crawlee/Impit HTTP transport against local counted redirect fixtures."""
import re
from urllib.parse import urlsplit

import pytest
from crawlee import Request
from crawlee.sessions import Session

from app.fetching.crawlee_http import PolicyHttpClient
from app.fetching.destinations import DestinationDenied, document_destination_allowed
from tests.integration.test_redirect_destinations import redirect_documents, authenticated_robots_redirects


@pytest.mark.parametrize("reason", ["action", "encoded", "scope", "robots", "cancelled"])
async def test_static_crawlee_fixture_denied_target_zero_hits(redirect_documents, monkeypatch, reason):
    origin, redirects, hits = redirect_documents
    target = {"action": "/cart?add-to-cart=42", "encoded": "/%63art", "scope": "/other",
              "robots": "/list/blocked", "cancelled": "/list/final"}[reason]
    redirects["/list/next?page=3"] = target
    async def public(url):
        return False
    monkeypatch.setattr("app.fetching.crawlee_http.url_is_private", public)
    async def allowed(request, url):
        return await document_destination_allowed(url, site_url=request.url,
            rules={"sensitive_path", "action_params"}, scope=[re.compile(r"/list(?:/|\?|$)")],
            robots_allowed=lambda u: urlsplit(u).path != "/list/blocked",
            cancelled=lambda: reason == "cancelled" and len(hits) >= 2)
    client = PolicyHttpClient(destination_policy=allowed)
    try:
        with pytest.raises(DestinationDenied):
            await client.crawl(Request.from_url(origin + "/list?page=2"))
        assert [path for path, _ in hits] == ["/list?page=2", "/list/next?page=3"]
        assert target not in [path for path, _ in hits]
    finally:
        await client.cleanup()


async def test_static_crawlee_fixture_allowed_identity_cookie_headers(redirect_documents, monkeypatch):
    origin, redirects, hits = redirect_documents
    redirects["/list/next?page=3"] = "/list/final?edition=mono"
    async def public(url):
        return False
    monkeypatch.setattr("app.fetching.crawlee_http.url_is_private", public)
    client = PolicyHttpClient(destination_policy=lambda request, url: True)
    try:
        request = Request.from_url(origin + "/list?page=2", headers={"X-Fixture": "retained"})
        result = await client.crawl(request, session=Session())
        assert result.http_response.status_code == 200
        assert request.loaded_url == origin + "/list/final?edition=mono"
        assert [path for path, _ in hits] == ["/list?page=2", "/list/next?page=3", "/list/final?edition=mono"]
        assert all({key.lower(): value for key, value in headers.items()}.get("x-fixture") == "retained" for _, headers in hits)
        assert "redirect_cookie=kept" in {key.lower(): value for key, value in hits[2][1].items()}.get("cookie", "")
    finally:
        await client.cleanup()


async def test_static_crawlee_header_dependent_robots_miss_bypasses_worker_queue(authenticated_robots_redirects, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from app.jobs.runner import JobRunner

    origin, hits = authenticated_robots_redirects
    async def public(url):
        return False
    monkeypatch.setattr("app.fetching.crawlee_http.url_is_private", public)
    async def blocked_queue(*args, **kwargs):
        raise AssertionError("Robots must not consume an occupied crawler worker")
    runner = SimpleNamespace(config={"respect_robots": True, "headers": {"X-Robots-Key": "retained"}},
        robots={}, aux_fetch=blocked_queue, throttle=SimpleNamespace(set_crawl_delay=lambda *a: None))
    async def allowed(request, url):
        if request.user_data.get("aux"):
            return True
        return await JobRunner._http_redirect_robots_allowed(runner, url)
    client = PolicyHttpClient(destination_policy=allowed)
    runner._redirect_http_client = client
    try:
        async with asyncio.timeout(3):
            with pytest.raises(DestinationDenied):
                await client.crawl(Request.from_url(origin + "/list"), session=Session())
        paths = [path for path, _ in hits]
        assert paths.count("/robots.txt") == 1
        assert "/list/next" in paths and "/list/final" not in paths
        assert {key.lower(): value for key, value in next(headers for path, headers in hits if path == "/robots.txt").items()}["x-robots-key"] == "retained"
    finally:
        await client.cleanup()
