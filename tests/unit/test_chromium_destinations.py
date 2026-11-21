"""Pure diagnostics for the isolated CDP prototype; no browser launch."""
from types import SimpleNamespace
import pytest

from app.fetching.chromium_destinations import ChromiumDocumentGuard


@pytest.mark.parametrize("target, expected", [("/cart?add-to-cart=42", "Fetch.failRequest"),
                                               ("/list/next?edition=mono", "Fetch.continueRequest")])
async def test_cdp_paused_redirect_decides_before_native_follow(monkeypatch, target, expected):
    sent = []
    async def send(method, params):
        sent.append((method, params))
    async def public(url):
        return False
    monkeypatch.setattr("app.fetching.chromium_destinations.url_is_private", public)
    guard = ChromiumDocumentGuard(object(), "https://catalogue.example.test/list")
    guard.main_frame_id = "main"
    guard.session = SimpleNamespace(send=send)
    await guard._response({"requestId": "paused", "networkId": "request", "frameId": "main",
        "request": {"url": "https://catalogue.example.test/list"}, "responseStatusCode": 302,
        "responseHeaders": [{"name": "Location", "value": target}]})
    assert [method for method, _ in sent] == [expected]


@pytest.mark.parametrize("private, expected", [(False, "Fetch.continueRequest"), (True, "Fetch.failRequest")])
async def test_cdp_iframe_redirect_checks_private_only(monkeypatch, private, expected):
    sent, policies = [], []
    async def send(method, params):
        sent.append((method, params))
    async def is_private(url):
        return private
    async def policy(url):
        policies.append(url)
        return False
    monkeypatch.setattr("app.fetching.chromium_destinations.url_is_private", is_private)
    guard = ChromiumDocumentGuard(object(), "https://catalogue.example.test/list", destination_policy=policy)
    guard.main_frame_id = "main"
    guard.session = SimpleNamespace(send=send)
    await guard._response({"requestId": "paused", "networkId": "request", "frameId": "challenge-frame",
        "request": {"url": "https://challenge.example.test/start"}, "responseStatusCode": 302,
        "responseHeaders": [{"name": "Location", "value": "https://challenge.example.test/final"}]})
    assert [method for method, _ in sent] == [expected]
    assert policies == []


async def test_cdp_close_revokes_waiting_policy_before_page_close(monkeypatch):
    import asyncio
    sent = []
    entered, release, close_entered, close_release = (asyncio.Event() for _ in range(4))
    async def send(method, params):
        sent.append(method)
    async def public(url):
        return False
    async def policy(url):
        entered.set()
        await release.wait()
        return True
    async def close():
        close_entered.set()
        await close_release.wait()
    monkeypatch.setattr("app.fetching.chromium_destinations.url_is_private", public)
    page = SimpleNamespace(is_closed=lambda: False, close=close)
    guard = ChromiumDocumentGuard(page, "https://catalogue.example.test/list", destination_policy=policy)
    guard.main_frame_id = "main"
    guard.session = SimpleNamespace(send=send, remove_listener=lambda *a: None, detach=lambda: None)
    event = {"requestId": "paused", "networkId": "request", "frameId": "main",
        "request": {"url": "https://catalogue.example.test/list"}, "responseStatusCode": 302,
        "responseHeaders": [{"name": "Location", "value": "/list/next"}]}
    task = asyncio.create_task(guard._response(event))
    await entered.wait()
    closing = asyncio.create_task(guard.close())
    await close_entered.wait()
    assert guard.closed is True
    release.set()
    await task
    assert "Fetch.continueRequest" not in sent
    close_release.set()
    await closing


async def test_cdp_partial_start_failure_can_close():
    async def close():
        pass
    guard = ChromiumDocumentGuard(SimpleNamespace(is_closed=lambda: False, close=close), "https://catalogue.example.test/list")
    await guard.close()
    assert guard.closed
