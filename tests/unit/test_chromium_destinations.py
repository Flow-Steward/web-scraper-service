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
