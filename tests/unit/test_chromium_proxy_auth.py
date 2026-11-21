"""Native Chromium auth uses explicit Proxy challenges, never ambiguous error text."""
import asyncio
from types import SimpleNamespace

import pytest
from crawlee import Request

from app.fetching.chromium_destinations import ChromiumDocumentGuard
from app.fetching.proxy_auth import ProxyAuthenticationRequired

FORMATTED_USER = "user-account-country-lv-city-riga-session-stickysession-duration-10"
PROXY = {"server": "http://proxy.fixture:8080", "username": FORMATTED_USER, "password": "fixture-password"}


def challenge(source="Proxy", request_id="auth"):
    return {"requestId": request_id, "request": {"url": "https://shop.test/product"},
            "authChallenge": {"source": source, "origin": "http://proxy.fixture:8080", "scheme": "basic", "realm": "fixture"}}


@pytest.mark.parametrize("source", ["Server", None])
async def test_origin_auth_never_receives_proxy_credentials(source):
    sent = []
    async def send(method, params):
        sent.append((method, params))
    guard = ChromiumDocumentGuard(object(), "https://shop.test/product", proxy_auth=PROXY)
    guard.session = SimpleNamespace(send=send)
    await guard._auth(challenge(source))
    assert sent == [("Fetch.continueWithAuth", {"requestId": "auth", "authChallengeResponse": {"response": "Default"}})]
    assert guard.errors == []


async def test_proxy_challenge_preserves_formatted_username_and_attempts_once():
    sent, rejected = [], []
    async def send(method, params):
        sent.append((method, params))
    guard = ChromiumDocumentGuard(object(), "https://shop.test/product", proxy_auth=PROXY,
                                  on_proxy_auth_required=lambda: rejected.append(True))
    guard.session = SimpleNamespace(send=send)
    await guard._auth(challenge())
    await guard._auth(challenge())
    assert sent[0] == ("Fetch.continueWithAuth", {"requestId": "auth", "authChallengeResponse": {
        "response": "ProvideCredentials", "username": FORMATTED_USER, "password": "fixture-password"}})
    assert sent[1][1]["authChallengeResponse"] == {"response": "CancelAuth"}
    assert rejected == [True]
    assert isinstance(guard.errors[0], ProxyAuthenticationRequired)
    assert "fixture-password" not in str(guard.errors)


async def test_ip_allowlist_rejection_reports_explicit_proxy_auth_without_credentials():
    sent = []
    async def send(method, params):
        sent.append((method, params))
    guard = ChromiumDocumentGuard(object(), "https://shop.test/product", proxy_auth={"server": "http://proxy.fixture:8080"})
    guard.session = SimpleNamespace(send=send)
    await guard._auth(challenge())
    assert sent[0][1]["authChallengeResponse"] == {"response": "CancelAuth"}
    assert isinstance(guard.errors[0], ProxyAuthenticationRequired)


async def test_old_generation_auth_cannot_send_credentials_or_poison_new_request():
    sent = []
    async def send(method, params):
        sent.append((method, params))
    guard = ChromiumDocumentGuard(object(), "https://shop.test/product", proxy_auth=PROXY)
    guard.session = SimpleNamespace(send=send)
    await guard.rebind("https://shop.test/next", proxy_auth=PROXY)
    await guard._auth(challenge(), generation=0)
    assert sent == []
    assert guard.errors == []


async def test_request_stage_does_not_clear_redirect_chain_state():
    sent = []
    async def send(method, params):
        sent.append((method, params))
    guard = ChromiumDocumentGuard(object(), "https://shop.test/product")
    guard.session = SimpleNamespace(send=send)
    key = (id(guard.session), "chain")
    guard.chains[key] = 7
    await guard._response({"requestId": "request", "networkId": "chain", "frameId": "main",
                           "request": {"url": "https://shop.test/next"}})
    assert guard.chains == {key: 7}
    assert sent == [("Fetch.continueRequest", {"requestId": "request"})]


async def test_final_407_is_explicit_rejection_and_401_is_not():
    async def send(method, params):
        pass
    for status in (401, 407):
        rejected = []
        guard = ChromiumDocumentGuard(object(), "https://shop.test/product", proxy_auth=PROXY,
                                      on_proxy_auth_required=lambda: rejected.append(True))
        guard.session = SimpleNamespace(send=send)
        await guard._response({"requestId": "request", "frameId": "main", "request": {"url": "https://shop.test/product"},
                               "responseStatusCode": status})
        assert bool(rejected) == (status == 407)
        assert any(isinstance(exc, ProxyAuthenticationRequired) for exc in guard.errors) == (status == 407)


async def test_job_guard_uses_corroborated_407_instead_of_generic_chromium_error(tmp_path, monkeypatch):
    from app.fetching.proxies import TierPlan
    from app.jobs.proxy_auth import guard_proxy_auth
    from tests.unit.test_proxy_auth_pause import make_run
    svc, runner = await make_run(tmp_path, monkeypatch)
    runner.tiers = TierPlan([{"tier": 1}])
    request = Request.from_url("https://target.test/product")
    request.user_data["_wss_proxy_auth_failed"] = True
    calls = []
    async def original(context):
        raise RuntimeError("net::ERR_HTTP_RESPONSE_CODE_FAILURE")
    async def original_error(context, error):
        calls.append("failure consumed")
    crawler = SimpleNamespace(_run_request_handler=original, _handle_request_error=original_error)
    guard_proxy_auth(crawler, runner, adaptive=False)
    try:
        from crawlee.errors import ContextPipelineInitializationError
        with pytest.raises(ContextPipelineInitializationError):
            await crawler._run_request_handler(SimpleNamespace(request=request))
        assert runner.state == "waiting_for_credentials"
        assert "_wss_proxy_auth_failed" not in request.user_data
        assert calls == []
    finally:
        svc.db.close()


async def test_native_auth_error_does_not_disable_retries_after_resume():
    from app.jobs.runner import raise_document_policy_error
    error = ProxyAuthenticationRequired()
    def rejected():
        raise error
    ctx = SimpleNamespace(request=SimpleNamespace(no_retry=False),
        page=SimpleNamespace(_wss_native_document_policy=SimpleNamespace(errors=[error], raise_if_denied=rejected)))
    with pytest.raises(ProxyAuthenticationRequired):
        raise_document_policy_error(ctx)
    assert ctx.request.no_retry is False


async def test_auth_from_redirect_policy_reaches_explicit_request_evidence(monkeypatch):
    rejected = []
    async def public(_):
        return False
    async def policy(_):
        raise ProxyAuthenticationRequired()
    async def send(method, params):
        pass
    monkeypatch.setattr("app.fetching.chromium_destinations.url_is_private", public)
    guard = ChromiumDocumentGuard(object(), "https://shop.test/product", proxy_auth=PROXY,
        on_proxy_auth_required=lambda: rejected.append(True), destination_policy=policy)
    guard.session, guard.main_frame_id = SimpleNamespace(send=send), "main"
    await guard._response({"requestId": "redirect", "frameId": "main", "networkId": "request", "request": {"url": "https://shop.test/product"},
        "responseStatusCode": 302, "responseHeaders": [{"name": "Location", "value": "/next"}]})
    assert rejected == [True]
    assert isinstance(guard.errors[0], ProxyAuthenticationRequired)


async def test_crawlee_binding_passes_actual_formatted_proxy_credentials(tmp_path, monkeypatch):
    from app.fetching.proxies import TierPlan
    from tests.unit.test_proxy_auth_pause import make_run
    svc, runner = await make_run(tmp_path, monkeypatch)
    runner.browser = "chromium"
    runner.tiers = TierPlan([{"tier": 1}])
    captured = {}
    async def bind(*args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(request=lambda route: None)
    async def route(*args):
        pass
    async def wait(*args):
        pass
    monkeypatch.setattr("app.fetching.document_policy.bind_document_policy", bind)
    monkeypatch.setattr(runner.throttle, "wait", wait)
    request = Request.from_url("https://target.test/product")
    info = SimpleNamespace(scheme="http", hostname="proxy.fixture", port=8080, username=FORMATTED_USER, password="fixture-password", url="http://proxy.fixture:8080")
    try:
        await runner._pre_nav(SimpleNamespace(request=request, page=SimpleNamespace(route=route), proxy_info=info))
        assert captured["proxy_auth"] == PROXY
        captured["on_proxy_auth_required"]()
        assert request.user_data["_wss_proxy_auth_failed"] is True
    finally:
        svc.db.close()
