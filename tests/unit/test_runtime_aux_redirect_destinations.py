"""The runtime browser's raw auxiliary read must not auto-follow redirects."""
from types import SimpleNamespace

from app.jobs.runner import JobRunner


async def test_runtime_browser_auxiliary_redirect_applies_destination_policy(monkeypatch):
    url = "https://catalogue.example.test/sitemap.xml"
    hits, results = [], []

    async def public(url):
        return False

    async def settle(*args):
        return True

    async def detect(*args):
        return SimpleNamespace(is_block=False, is_challenge=False)

    async def body():
        return b"Action endpoint"

    async def dispose():
        pass

    async def get(target, **kwargs):
        hits.append(target)
        return SimpleNamespace(status=302, url=target,
            headers={"location": "/cart?add-to-cart=42"}, body=body, dispose=dispose)

    monkeypatch.setattr("app.fetching.oneoff.url_is_private", public)
    monkeypatch.setattr("app.fetching.challenge.settle", settle)
    monkeypatch.setattr("app.jobs.runner.detect_page", detect)
    instance = SimpleNamespace(config={}, state="running", tiers=SimpleNamespace(enabled=False),
                               _shutdown=False, _cut_or_stopping=lambda: False,
                               _aux_done=lambda request, result: results.append(result))
    ctx = SimpleNamespace(request=SimpleNamespace(url=url),
                          page=SimpleNamespace(context=SimpleNamespace(request=SimpleNamespace(get=get))))
    await JobRunner._handle_aux(instance, ctx, "catalogue.example.test", 200, True)
    assert results[0].error == "redirect_policy_denied"
    assert hits == [url]


import pytest


@pytest.mark.parametrize("transport_error", [False, True])
async def test_guarded_context_raw_proxy_auth_retains_sanitized_pause_signal(monkeypatch, transport_error):
    from app.fetching.oneoff import context_get
    from app.fetching.proxy_auth import ProxyAuthenticationRequired
    from playwright.async_api import Error

    async def public(url):
        return False
    async def body():
        return b""
    async def dispose():
        pass
    async def get(*args, **kwargs):
        if transport_error:
            raise Error("NS_ERROR_PROXY_AUTHENTICATION_FAILED fixture-password")
        return SimpleNamespace(status=407, url=args[0], headers={}, body=body, dispose=dispose)
    monkeypatch.setattr("app.fetching.oneoff.url_is_private", public)
    context = SimpleNamespace(request=SimpleNamespace(get=get))
    with pytest.raises(ProxyAuthenticationRequired) as caught:
        await context_get(context, "https://catalogue.test/sitemap.xml", proxy_used=True)
    assert "fixture-password" not in str(caught.value)


@pytest.mark.parametrize("transport_error", [False, True])
async def test_browser_session_raw_retains_proxy_auth_error_code(monkeypatch, transport_error):
    from app.fetching.oneoff import BrowserSession
    from playwright.async_api import Error

    async def public(url):
        return False
    async def body():
        return b""
    async def dispose():
        pass
    async def get(*args, **kwargs):
        if transport_error:
            raise Error("NS_ERROR_PROXY_AUTHENTICATION_FAILED fixture-password")
        return SimpleNamespace(status=407, url=args[0], headers={}, body=body, dispose=dispose)
    monkeypatch.setattr("app.fetching.oneoff.url_is_private", public)
    session = BrowserSession("camoufox", proxy="http://fixture.invalid:8000")
    session.context = SimpleNamespace(request=SimpleNamespace(get=get))
    result = await session.raw("https://catalogue.test/sitemap.xml")
    assert result.error == "proxy_auth_failed"


@pytest.mark.parametrize("transport_error", [False, True])
async def test_runtime_redirect_hop_proxy_auth_pauses_without_target_completion(tmp_path, monkeypatch, transport_error):
    from app.fetching.proxies import TierPlan
    from app.jobs.runner import Deferred
    from playwright.async_api import Error
    from crawlee import Request
    from tests.unit.test_proxy_auth_pause import make_run

    svc, runner = await make_run(tmp_path, monkeypatch)
    runner.tiers = TierPlan([{"tier": 1}])
    request = Request.from_url("https://target.test/sitemap.xml", user_data={"aux": True})
    hits = []
    async def public(url):
        return False
    async def settled(*args):
        return True
    async def verdict(*args):
        return SimpleNamespace(is_block=False, is_challenge=False)
    async def body():
        return b""
    async def dispose():
        pass
    async def get(url, **kwargs):
        hits.append(url)
        if url == request.url:
            return SimpleNamespace(status=302, url=url, headers={"location": "/redirected-sitemap.xml"}, body=body, dispose=dispose)
        if transport_error:
            raise Error("NS_ERROR_PROXY_AUTHENTICATION_FAILED fixture-password")
        return SimpleNamespace(status=407, url=url, headers={}, body=body, dispose=dispose)
    monkeypatch.setattr("app.fetching.oneoff.url_is_private", public)
    monkeypatch.setattr("app.fetching.challenge.settle", settled)
    monkeypatch.setattr("app.jobs.runner.detect_page", verdict)
    ctx = SimpleNamespace(request=request, page=SimpleNamespace(context=SimpleNamespace(request=SimpleNamespace(get=get))))
    try:
        with pytest.raises(Deferred):
            await runner._handle_aux(ctx, "target.test", 200, True)
        assert runner.state == "waiting_for_credentials"
        assert runner.prot()["hard_blocks"] == 0
        assert runner.prot()["challenges_met"] == 0
        assert hits == [request.url, "https://target.test/redirected-sitemap.xml"]
    finally:
        svc.db.close()


async def test_browser_raw_challenge_step_keeps_proxy_auth_signal(monkeypatch):
    from app.fetching.oneoff import BrowserSession
    from playwright.async_api import Error

    async def public(url):
        return False
    async def body():
        return b"Fixture challenge document"
    async def dispose():
        pass
    async def get(url, **kwargs):
        return SimpleNamespace(status=403, url=url, headers={}, body=body, dispose=dispose)
    async def lost_proxy(*args, **kwargs):
        raise Error("NS_ERROR_PROXY_AUTHENTICATION_FAILED fixture-password")
    monkeypatch.setattr("app.fetching.oneoff.url_is_private", public)
    monkeypatch.setattr("app.fetching.challenge.detect_html", lambda *args: SimpleNamespace(is_challenge=True))
    session = BrowserSession("camoufox", proxy="http://fixture.invalid:8000")
    session.context = SimpleNamespace(request=SimpleNamespace(get=get))
    monkeypatch.setattr(session, "get", lost_proxy)
    assert (await session.raw("https://catalogue.test/sitemap.xml")).error == "proxy_auth_failed"


async def test_guarded_context_does_not_label_direct_origin_407_as_proxy_auth(monkeypatch):
    from app.fetching.oneoff import context_get
    async def public(url):
        return False
    async def body():
        return b"Direct origin response"
    async def dispose():
        pass
    async def get(url, **kwargs):
        return SimpleNamespace(status=407, url=url, headers={}, body=body, dispose=dispose)
    monkeypatch.setattr("app.fetching.oneoff.url_is_private", public)
    result = await context_get(SimpleNamespace(request=SimpleNamespace(get=get)), "https://catalogue.test/sitemap.xml")
    assert result.status == 407 and result.error is None
