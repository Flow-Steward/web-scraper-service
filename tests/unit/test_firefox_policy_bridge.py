"""Native Firefox bridge rejects unbound or stale document authorizations."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from app.fetching.firefox_policy import FirefoxPolicyBridge


async def call(bridge, path, payload, *, token=None):
    async with httpx.AsyncClient(trust_env=False) as client:
        return await client.post(bridge.origin + path, json=payload,
                                 headers={"Authorization": "Bearer " + (token or bridge.token)})


async def test_bridge_rejects_wrong_token_and_unknown_tab():
    bridge = await FirefoxPolicyBridge().start()
    try:
        assert (await call(bridge, "/ready", {"privateAccess": True}, token="wrong")).status_code == 403
        assert (await call(bridge, "/ready", {"privateAccess": True})).status_code == 200
        response = await call(bridge, "/policy", {
            "tabId": 7, "storeId": "private-1", "generation": "unknown", "url": "https://shop.test/list",
            "frameKind": "main_frame", "requestId": "r1", "redirects": 0,
        })
        assert response.json()["allow"] is False
    finally:
        await bridge.close()


async def test_nonce_is_one_use_and_tab_generation_cannot_cross_context():
    bridge = await FirefoxPolicyBridge().start()
    seen = []
    async def policy(url, frame_kind, redirects):
        seen.append(url)
        return True
    guard = SimpleNamespace(page=SimpleNamespace(context=object()), generation="gen-1", active=True,
                            decide=policy, errors=[])
    try:
        nonce, enrolled = bridge.prepare(guard)
        payload = {"nonce": nonce, "tabId": 4, "storeId": "private-1"}
        response = await call(bridge, "/enroll", payload)
        assert response.json()["generation"] == "gen-1"
        assert not enrolled.done()
        assert (await call(bridge, "/bound", {"tabId": 4, "storeId": "private-1", "generation": "gen-1"})).json()["allow"] is True
        await enrolled
        assert (await call(bridge, "/enroll", payload)).json()["allow"] is False
        base = {"tabId": 4, "storeId": "private-1", "generation": "gen-1", "url": "https://shop.test/list",
                "frameKind": "main_frame", "requestId": "r1", "redirects": 0}
        assert (await call(bridge, "/policy", {**base, "tabId": 5})).json()["allow"] is False
        assert (await call(bridge, "/policy", {**base, "storeId": "private-2"})).json()["allow"] is False
        assert (await call(bridge, "/policy", {**base, "generation": "gen-old"})).json()["allow"] is False
        assert seen == []
    finally:
        await bridge.close()


async def test_launch_waits_for_policy_addon_and_cleans_failure(monkeypatch):
    import sys
    from pathlib import Path
    from types import ModuleType
    from app.fetching.browsers import launch_camoufox

    paths = []
    closed = []
    module = ModuleType("camoufox.async_api")
    async def launch(playwright, **options):
        paths.extend(options["addons"])
        closed.append(False)
        browser = SimpleNamespace(on=lambda *args: None)
        async def close(**kwargs):
            closed[0] = True
        browser.close = close
        return browser
    module.AsyncNewBrowser = launch
    monkeypatch.setitem(sys.modules, "camoufox", ModuleType("camoufox"))
    monkeypatch.setitem(sys.modules, "camoufox.async_api", module)
    async def fail_ready(self, timeout=10):
        raise TimeoutError("addon unavailable")
    monkeypatch.setattr(FirefoxPolicyBridge, "wait_ready", fail_ready)
    with pytest.raises(TimeoutError):
        await launch_camoufox(object(), headless=True)
    assert closed == [True]
    assert paths and not Path(paths[0]).exists()


async def bound_guard(bridge, policy):
    guard = SimpleNamespace(page=SimpleNamespace(context=object()), generation="current", active=True,
                            decide=policy, errors=[])
    nonce, enrolled = bridge.prepare(guard)
    assert (await call(bridge, "/enroll", {"nonce": nonce, "tabId": 1, "storeId": "private-1"})).json()["allow"]
    await call(bridge, "/bound", {"tabId": 1, "storeId": "private-1", "generation": "current"})
    await enrolled
    return guard, {"tabId": 1, "storeId": "private-1", "generation": "current", "url": "https://shop.test/list",
                   "frameKind": "main_frame", "requestId": "chain-1", "redirects": 0}


async def test_revoke_during_policy_never_releases_old_document():
    bridge = await FirefoxPolicyBridge().start()
    started, release = asyncio.Event(), asyncio.Event()
    async def policy(*args):
        started.set()
        await release.wait()
        return True
    try:
        guard, payload = await bound_guard(bridge, policy)
        response = asyncio.create_task(call(bridge, "/policy", payload))
        await asyncio.wait_for(started.wait(), 2)
        bridge.revoke(guard)
        guard.generation = "new"
        release.set()
        assert (await response).json()["allow"] is False
    finally:
        await bridge.close()


async def test_close_cancels_paused_policy_and_removes_addon_listener_tasks():
    from pathlib import Path
    bridge = await FirefoxPolicyBridge().start()
    path = Path(bridge.addon_path)
    started, cancelled = asyncio.Event(), asyncio.Event()
    async def policy(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    guard, payload = await bound_guard(bridge, policy)
    request = asyncio.create_task(call(bridge, "/policy", payload))
    await asyncio.wait_for(started.wait(), 2)
    await asyncio.wait_for(bridge.close(), 2)
    result = await asyncio.gather(request, return_exceptions=True)
    assert isinstance(result[0], httpx.HTTPError)
    assert cancelled.is_set() and not guard.active
    assert not path.exists() and not bridge._tasks and not bridge._writers
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", int(bridge.origin.rsplit(":", 1)[1]))


@pytest.mark.parametrize("change", [
    {"frameKind": "script"}, {"redirects": True}, {"redirects": -1},
    {"url": "x" * 8193}, {"requestId": []}, {"unexpected": "data"},
])
async def test_bridge_schema_failure_does_not_invoke_policy(change):
    bridge = await FirefoxPolicyBridge().start()
    seen = []
    async def policy(*args):
        seen.append(args)
        return True
    try:
        _, payload = await bound_guard(bridge, policy)
        assert (await call(bridge, "/policy", {**payload, **change})).status_code == 400
        assert seen == []
    finally:
        await bridge.close()


async def test_guard_only_applies_caller_policy_to_main_frame(monkeypatch):
    from app.fetching import firefox_policy
    async def public(url):
        return url.startswith("http://private.test")
    monkeypatch.setattr(firefox_policy, "url_is_private", public)
    seen = []
    async def caller(url):
        seen.append(url)
        return False
    guard = firefox_policy.FirefoxDocumentGuard(SimpleNamespace(context=SimpleNamespace(browser=None)),
                                              "https://shop.test/list", destination_policy=caller)
    guard.active = True
    assert await guard.decide("https://challenge.test/frame", "sub_frame", 0)
    assert not await guard.decide("http://private.test/frame", "sub_frame", 1)
    assert seen == []
    assert not await guard.decide("https://shop.test/list/blocked", "main_frame", 1)
    assert seen == ["https://shop.test/list/blocked"]


async def test_guard_policy_timeout_fails_closed(monkeypatch):
    from app.fetching import firefox_policy
    async def public(url):
        return False
    monkeypatch.setattr(firefox_policy, "url_is_private", public)
    async def paused(url):
        await asyncio.Event().wait()
    guard = firefox_policy.FirefoxDocumentGuard(SimpleNamespace(context=SimpleNamespace(browser=None)),
                                              "https://shop.test/list", destination_policy=paused, timeout_s=0.01)
    guard.active = True
    assert not await guard.decide("https://shop.test/list/next", "main_frame", 1)
    assert isinstance(guard.errors[0], TimeoutError)


async def test_connection_limit_rejects_without_more_tasks():
    bridge = await FirefoxPolicyBridge().start()
    clients = []
    try:
        for _ in range(bridge.MAX_CONNECTIONS + 1):
            clients.append(await asyncio.open_connection("127.0.0.1", int(bridge.origin.rsplit(":", 1)[1])))
        await asyncio.sleep(0)
        assert len(bridge._tasks) <= bridge.MAX_CONNECTIONS
        assert await asyncio.wait_for(clients[-1][0].read(1), 1) == b""
    finally:
        for _, writer in clients:
            writer.close()
            await writer.wait_closed()
        await bridge.close()


async def test_old_policy_error_does_not_contaminate_rebound_generation(monkeypatch):
    from app.fetching import firefox_policy
    async def public(url):
        return False
    monkeypatch.setattr(firefox_policy, "url_is_private", public)
    started, finish = asyncio.Event(), asyncio.Event()
    async def delayed(url):
        started.set()
        await finish.wait()
        return False
    guard = firefox_policy.FirefoxDocumentGuard(SimpleNamespace(context=SimpleNamespace(browser=None)),
                                              "https://shop.test/list", destination_policy=delayed)
    guard.active = True
    pending = asyncio.create_task(guard.decide("https://shop.test/list/next", "main_frame", 0))
    await asyncio.wait_for(started.wait(), 1)
    guard.generation = "rebound"
    finish.set()
    assert await pending is False
    assert guard.errors == []


def test_raise_if_denied_preserves_original_error():
    from app.fetching.firefox_policy import FirefoxDocumentGuard
    from app.fetching.destinations import DestinationDenied
    guard = FirefoxDocumentGuard(SimpleNamespace(context=SimpleNamespace(browser=None)), "https://shop.test/list")
    guard.raise_if_denied()
    denied = DestinationDenied("https://shop.test/cart")
    guard.errors.append(denied)
    with pytest.raises(DestinationDenied) as raised:
        guard.raise_if_denied()
    assert raised.value is denied


async def test_original_admission_remains_caller_owned_but_redirect_return_checked(monkeypatch):
    from app.fetching import firefox_policy
    async def public(url):
        return False
    monkeypatch.setattr(firefox_policy, "url_is_private", public)
    calls = []
    async def policy(url):
        calls.append(url)
        return False
    initial = "https://shop.test/list"
    guard = firefox_policy.FirefoxDocumentGuard(SimpleNamespace(context=SimpleNamespace(browser=None)), initial,
                                              destination_policy=policy)
    guard.active = True
    assert await guard.decide(initial, "main_frame", 0)
    assert calls == []
    assert not await guard.decide(initial, "main_frame", 2)
    assert calls == [initial]
