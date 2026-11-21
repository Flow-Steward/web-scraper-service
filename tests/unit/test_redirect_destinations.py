"""Redirect destinations must be rejected before their transport sees a request."""
from functools import partial

import httpx
import pytest

from app.fetching.oneoff import http_get

ORIGIN = "https://catalogue.example.test"


@pytest.mark.parametrize("target", ["/cart?add-to-cart=42", "/item?add-to-cart=42", "/%63art", "/%6cogout", "https://other.example.test/list"])
async def test_http_redirect_denied_destination_has_zero_hits(monkeypatch, target):
    hits = []

    def respond(request):
        hits.append(str(request.url))
        if request.url.path == "/list":
            return httpx.Response(302, headers={"location": target})
        return httpx.Response(200, text="Action endpoint")

    client = httpx.AsyncClient
    monkeypatch.setattr("app.fetching.oneoff.httpx.AsyncClient", partial(client, transport=httpx.MockTransport(respond)))
    result = await http_get(ORIGIN + "/list", check_private=False)
    assert hits == [ORIGIN + "/list"]
    assert result.error == "redirect_policy_denied"


async def test_http_redirect_rechecks_caller_policy_and_keeps_final_identity(monkeypatch):
    hits, checked = [], []

    def respond(request):
        hits.append(str(request.url))
        if request.url.path == "/list":
            return httpx.Response(302, headers={"location": "/list/next?edition=mono"})
        return httpx.Response(200, text="Listing")

    async def allowed(url):
        checked.append(url)
        return True

    client = httpx.AsyncClient
    monkeypatch.setattr("app.fetching.oneoff.httpx.AsyncClient", partial(client, transport=httpx.MockTransport(respond)))
    result = await http_get(ORIGIN + "/list", check_private=False, destination_policy=allowed)
    assert result.final_url == ORIGIN + "/list/next?edition=mono"
    assert result.text == "Listing"
    assert checked == [result.final_url]
    assert hits == [ORIGIN + "/list", result.final_url]


@pytest.mark.parametrize("reason", ["scope", "robots", "cancelled", "actions"])
async def test_document_policy_enforces_caller_rules(reason):
    import re
    from app.fetching.destinations import document_destination_allowed

    async def robots(url):
        return reason != "robots"

    url = ORIGIN + ("/cart?add-to-cart=42" if reason == "actions" else "/list/next")
    assert not await document_destination_allowed(
        url, site_url=ORIGIN, rules={"sensitive_path", "action_params"},
        scope=[re.compile(r"/only-this$")] if reason == "scope" else (),
        robots_allowed=robots, cancelled=lambda: reason == "cancelled",
    )


@pytest.mark.parametrize("kind", ["caller", "cancel", "private"])
async def test_http_redirect_caller_cancellation_and_private_guards(monkeypatch, kind):
    import asyncio
    from app.fetching.oneoff import PrivateAddress

    hits = []
    def respond(request):
        hits.append(str(request.url))
        return httpx.Response(302, headers={"location": "/list/next"})

    async def policy(url):
        if kind == "cancel":
            raise asyncio.CancelledError
        return kind != "caller"

    async def private(url):
        return kind == "private" and url.endswith("/next")

    monkeypatch.setattr("app.fetching.oneoff.url_is_private", private)
    monkeypatch.setattr("app.fetching.oneoff.guarded_transport", lambda **kw: httpx.MockTransport(respond))
    if kind == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await http_get(ORIGIN + "/list", destination_policy=policy)
    elif kind == "private":
        with pytest.raises(PrivateAddress):
            await http_get(ORIGIN + "/list", destination_policy=policy)
    else:
        assert (await http_get(ORIGIN + "/list", destination_policy=policy)).error == "redirect_policy_denied"
    assert hits == [ORIGIN + "/list"]


async def test_browser_raw_redirect_denied_before_target_request(monkeypatch):
    from types import SimpleNamespace
    from app.fetching.oneoff import BrowserSession

    async def public(url):
        return False

    hits = []
    async def dispose():
        pass

    async def get(url, **kw):
        hits.append(url)
        return SimpleNamespace(status=302, headers={"location": "/cart?add-to-cart=42"}, url=url,
                               body=lambda: None, dispose=dispose)

    monkeypatch.setattr("app.fetching.oneoff.url_is_private", public)
    session = BrowserSession("chromium")
    session.context = SimpleNamespace(request=SimpleNamespace(get=get))
    result = await session.raw(ORIGIN + "/list")
    assert result.error == "redirect_policy_denied"
    assert hits == [ORIGIN + "/list"]
