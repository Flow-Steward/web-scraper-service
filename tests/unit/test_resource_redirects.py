"""Native redirects must not evade optional-resource filtering."""
from types import SimpleNamespace

import pytest

from app.fetching.chromium_destinations import ChromiumDocumentGuard
from app.fetching.firefox_policy import FirefoxPolicyBridge
from tests.unit.test_firefox_policy_bridge import bound_guard, call


@pytest.mark.parametrize("kind", ["Script", "Document"])
@pytest.mark.parametrize("enabled,expected", [(True, "Fetch.failRequest"), (False, "Fetch.continueRequest")])
async def test_chromium_optional_redirect_is_blocked_without_document_error(monkeypatch, kind, enabled, expected):
    async def public(url):
        return False
    monkeypatch.setattr("app.fetching.chromium_destinations.url_is_private", public)
    calls = []
    async def send(method, params):
        calls.append(method)
    guard = ChromiumDocumentGuard(SimpleNamespace(url="https://catalogue.test/product"), "https://catalogue.test/product")
    guard.resource_economy_enabled = enabled
    guard.main_frame_id = "main"
    guard.session = SimpleNamespace(send=send)
    await guard._response({"requestId": "redirect", "networkId": "request", "frameId": "iframe" if kind == "Document" else "main",
                           "resourceType": kind, "request": {"url": "https://cdn.catalogue.test/redirect"},
                           "responseStatusCode": 302, "responseHeaders": [{"name": "Location", "value": "https://embed.tawk.to/widget"}]})
    assert calls == [expected]
    assert guard.errors == []


@pytest.mark.parametrize("enabled,want", [(True, False), (False, True)])
async def test_firefox_optional_resource_redirect_is_not_a_private_address_failure(monkeypatch, enabled, want):
    async def public(url):
        return False
    monkeypatch.setattr("app.fetching.firefox_policy.url_is_private", public)
    bridge = await FirefoxPolicyBridge().start()
    try:
        async def allowed(*args):
            return True
        guard, bound = await bound_guard(bridge, allowed)
        guard.page.url = "https://catalogue.test/product"
        guard.initial_url = guard.page.url
        guard.resource_economy_enabled = enabled
        response = await call(bridge, "/private-redirect", {k: bound[k] for k in ["tabId", "storeId", "generation"]}
                              | {"url": "https://embed.tawk.to/widget"})
        assert response.json()["allow"] is want
        assert guard.errors == []
    finally:
        await bridge.close()


@pytest.mark.parametrize("committed,want", [("https://tawk.to/product", False), ("https://catalogue.test/product", True)])
def test_native_resource_policy_uses_effective_document(committed, want):
    from app.fetching.resources import blocks_native_resource

    guard = SimpleNamespace(resource_economy_enabled=True, page=SimpleNamespace(url=committed), initial_url="https://other.test/start")
    assert blocks_native_resource(guard, "https://embed.tawk.to/widget") is want
