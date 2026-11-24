"""The native form allowance cannot become a general POST permission."""
from types import SimpleNamespace

import pytest

from app.fetching.challenge_forms import ChallengeFormAllowance
from app.fetching.firefox_policy import FirefoxDocumentGuard

URL = "https://shop.test/product"
HEADERS = {"server": "cloudflare", "cf-mitigated": "challenge", "cf-ray": "fixture-ray"}


def armed():
    allowance = ChallengeFormAllowance(URL, True)
    allowance.get_started("get")
    assert allowance.observe(URL, "get", 403, HEADERS)
    return allowance


def test_one_native_request_can_repeat_interception_but_not_send_a_new_post():
    allowance = armed()
    assert allowance.reserve(URL, "post", 200)
    assert allowance.reserve(URL, "post", 200)
    assert not allowance.reserve(URL, "another-post", 200)
    assert not allowance.reserve(URL, "post", 201)
    assert not allowance.observe(URL, "get", 403, HEADERS)
    assert not allowance.reserve(URL, "third-post", 200)


def test_final_wire_release_is_one_use_and_checks_expiry(monkeypatch):
    allowance = armed()
    assert allowance.reserve(URL, "post", 200)
    assert not allowance.release(URL, "other", 200)
    assert not allowance.release(URL, "post", 201)
    assert allowance.release(URL, "post", 200)
    assert not allowance.release(URL, "post", 200)
    assert not allowance.reserve(URL, "post", 200)
    allowance = armed()
    assert allowance.reserve(URL, "post", 200)
    monkeypatch.setattr("app.fetching.challenge_forms.time.monotonic", lambda: allowance.deadline)
    assert not allowance.release(URL, "post", 200)


@pytest.mark.parametrize("enabled,url,status,headers", [
    (False, URL, 403, HEADERS),
    (True, URL.replace("https:", "http:"), 403, HEADERS),
    (True, URL, 200, HEADERS),
    (True, URL, 403, {**HEADERS, "server": "origin"}),
    (True, URL, 403, {**HEADERS, "cf-mitigated": None}),
    (True, URL, 403, {**HEADERS, "cf-ray": None}),
])
def test_unqualified_response_does_not_authorize_a_form(enabled, url, status, headers):
    allowance = ChallengeFormAllowance(url, enabled)
    allowance.get_started("get")
    assert not allowance.observe(url, "get", status, headers)
    assert not allowance.reserve(url, "post", 200)


@pytest.mark.parametrize("size", [None, False, True, 0, -1, 65_537, "200"])
def test_missing_or_invalid_form_metadata_is_denied(size):
    assert not armed().reserve(URL, "post", size)


def test_url_request_identity_expiry_and_intervening_navigation(monkeypatch):
    allowance = armed()
    assert not allowance.reserve(URL + "?action=buy", "post", 200)
    assert not allowance.observe(URL, "other-get", 403, HEADERS)
    allowance.get_started("new-navigation")
    assert not allowance.reserve(URL, "post", 200)
    assert not allowance.observe(URL, "get", 403, HEADERS)
    allowance = armed()
    monkeypatch.setattr("app.fetching.challenge_forms.time.monotonic", lambda: allowance.deadline)
    assert not allowance.reserve(URL, "post", 200)


async def test_async_denial_burns_reservation_and_does_not_allow_replay(monkeypatch):
    async def public(url):
        return False
    decisions = iter([False, True])
    async def denied(url):
        return next(decisions)
    monkeypatch.setattr("app.fetching.firefox_policy.url_is_private", public)
    guard = FirefoxDocumentGuard(SimpleNamespace(context=SimpleNamespace(browser=None)), URL,
                                 destination_policy=denied, challenge_post_allowed=True)
    guard.active = True
    guard.challenge_form = armed()
    assert not await guard.decide(URL, "main_frame", 0, "POST", request_id="post", form_bytes=200)
    assert guard.challenge_form.used
    assert not await guard.decide(URL, "main_frame", 0, "POST", request_id="post", form_bytes=200)
    assert not await guard.decide(URL, "main_frame", 0, "POST", request_id="other", form_bytes=200)


async def test_post_reservation_precedes_async_private_check(monkeypatch):
    import asyncio
    entered, release = asyncio.Event(), asyncio.Event()
    async def public(url):
        entered.set()
        await release.wait()
        return False
    monkeypatch.setattr("app.fetching.firefox_policy.url_is_private", public)
    guard = FirefoxDocumentGuard(SimpleNamespace(context=SimpleNamespace(browser=None)), URL, challenge_post_allowed=True)
    guard.active = True
    guard.challenge_form = armed()
    first = asyncio.create_task(guard.decide(URL, "main_frame", 0, "POST", request_id="first", form_bytes=200))
    await entered.wait()
    assert not await guard.decide(URL, "main_frame", 0, "POST", request_id="second", form_bytes=200)
    release.set()
    assert not await first  # The competing denied navigation revokes the first.


async def test_cancellation_revokes_same_request_id(monkeypatch):
    import asyncio
    entered = asyncio.Event()
    async def pending(url):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr("app.fetching.firefox_policy.url_is_private", pending)
    guard = FirefoxDocumentGuard(SimpleNamespace(context=SimpleNamespace(browser=None)), URL, challenge_post_allowed=True)
    guard.active = True
    guard.challenge_form = armed()
    first = asyncio.create_task(guard.decide(URL, "main_frame", 0, "POST", request_id="post", form_bytes=200))
    await entered.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not guard.challenge_form.reserve(URL, "post", 200)
    assert not guard.release_form(URL, "post", 200)
