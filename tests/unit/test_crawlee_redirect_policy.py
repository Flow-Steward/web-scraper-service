"""Real Crawlee Impit redirect machinery with a deterministic synthetic wire."""
from types import SimpleNamespace

import httpx
import pytest
from crawlee import Request
from crawlee.sessions import Session

ORIGIN = "https://catalogue.example.test"


def install_wire(monkeypatch, destination):
    from crawlee.http_clients import ImpitHttpClient
    hits = []
    async def request(**kwargs):
        url = kwargs["url"]
        hits.append((url, kwargs.get("headers") or {}))
        if url.endswith("/list"):
            status, headers, content = 302, {"location": destination, "set-cookie": "hop=kept; Path=/"}, b""
        else:
            status, headers, content = 200, {}, b"Allowed"
        return SimpleNamespace(url=url, status_code=status, headers=httpx.Headers(headers),
                               http_version="HTTP/1.1", is_closed=True, content=content)
    monkeypatch.setattr(ImpitHttpClient, "_get_client", lambda self, proxy: SimpleNamespace(request=request))
    async def public(url):
        return False
    monkeypatch.setattr("app.fetching.crawlee_http.url_is_private", public)
    return hits


@pytest.mark.parametrize("target", ["/cart?add-to-cart=42", "/%63art", "https://other.example.test/list"])
async def test_static_crawlee_denies_redirect_before_wire(monkeypatch, target):
    from app.fetching.crawlee_http import PolicyHttpClient
    from app.fetching.destinations import DestinationDenied
    hits = install_wire(monkeypatch, target)
    client = PolicyHttpClient(destination_policy=lambda req, url: True)
    original = Request.from_url(ORIGIN + "/list")
    with pytest.raises(DestinationDenied):
        await client.crawl(original)
    assert [url for url, _ in hits] == [original.url]
    assert original.no_retry is True


async def test_static_crawlee_allowed_redirect_keeps_session_headers_final_identity(monkeypatch):
    from app.fetching.crawlee_http import PolicyHttpClient
    checked = []
    hits = install_wire(monkeypatch, "/list/next?edition=mono")
    async def policy(request, url):
        checked.append((request.url, url))
        return True
    client = PolicyHttpClient(destination_policy=policy)
    original = Request.from_url(ORIGIN + "/list", headers={"X-Fixture": "retained"})
    result = await client.crawl(original, session=Session())
    final = ORIGIN + "/list/next?edition=mono"
    assert original.loaded_url == final
    assert original.url == ORIGIN + "/list"
    assert await result.http_response.read() == b"Allowed"
    assert checked == [(original.url, final)]
    assert hits[1][1]["cookie"] == "hop=kept"
    assert all(headers["x-fixture"] == "retained" for _, headers in hits)


async def test_static_crawlee_robots_auxiliary_reuses_session_without_crawler_slot(monkeypatch):
    from app.fetching.crawlee_http import PolicyHttpClient
    hits = install_wire(monkeypatch, "/list/next")
    checked = []
    async def policy(request, url):
        checked.append(url)
        robots = await client.fetch_auxiliary(ORIGIN + "/robots.txt")
        assert robots.status == 200
        return True
    client = PolicyHttpClient(destination_policy=policy)
    original = Request.from_url(ORIGIN + "/list")
    await client.crawl(original, session=Session())
    assert [url for url, _ in hits] == [original.url, ORIGIN + "/robots.txt", ORIGIN + "/list/next"]
    assert checked == [ORIGIN + "/list/next"]
    assert hits[1][1]["cookie"] == "hop=kept"


@pytest.mark.parametrize("reason", ["denied", "private", "timeout", "cancelled"])
async def test_static_crawlee_policy_private_and_cancellation_fail_before_wire(monkeypatch, reason):
    import asyncio
    from app.fetching.crawlee_http import PolicyHttpClient
    from app.fetching.destinations import DestinationDenied
    from app.fetching.oneoff import PrivateAddress
    hits = install_wire(monkeypatch, "/list/next")
    async def private(url):
        return reason == "private" and url.endswith("/next")
    monkeypatch.setattr("app.fetching.crawlee_http.url_is_private", private)
    async def policy(request, url):
        if reason == "timeout":
            await asyncio.Event().wait()
        if reason == "cancelled":
            raise asyncio.CancelledError
        return False
    client = PolicyHttpClient(destination_policy=policy, policy_timeout_s=0.01)
    error = {"denied": DestinationDenied, "private": PrivateAddress,
             "timeout": TimeoutError, "cancelled": asyncio.CancelledError}[reason]
    with pytest.raises(error):
        await client.crawl(Request.from_url(ORIGIN + "/list"))
    assert [url for url, _ in hits] == [ORIGIN + "/list"]
