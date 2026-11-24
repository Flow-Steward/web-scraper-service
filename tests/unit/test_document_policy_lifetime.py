"""Dispatcher owns one guard lifetime per page and rebinds sequential fetches."""
import asyncio
from types import SimpleNamespace

from app.fetching.document_policy import NativeDocumentPolicy, bind_document_policy
import pytest


async def test_native_policy_reuses_binding_and_closes_once(monkeypatch):
    events, starts, bindings, closes = {}, [], [], []
    page = SimpleNamespace(on=lambda name, callback: events.update({name: callback}))
    class Guard:
        errors = []
        def __init__(self, page, initial_url, **kwargs):
            self.page = page
        async def start(self):
            starts.append(self)
        async def rebind(self, url, callback, **kwargs):
            bindings.append((url, callback))
        async def close(self):
            closes.append(self)
            events["close"]()
        async def request(self, route):
            pass
    monkeypatch.setattr("app.fetching.chromium_destinations.ChromiumDocumentGuard", Guard)
    first = await bind_document_policy(page, "chromium", "https://catalogue.test/list")
    second = await bind_document_policy(page, "chromium", "https://catalogue.test/list/next")
    assert first is second and len(starts) == 1
    assert bindings == [("https://catalogue.test/list/next", None)]
    await asyncio.gather(first.close(), second.close())
    assert len(closes) == 1


def response_fixture():
    events = {}
    page = SimpleNamespace(url="https://catalogue.test/map.xml", main_frame=SimpleNamespace(url="https://catalogue.test/map.xml"),
                           on=lambda name, callback: events.update({name: callback}))
    async def noop(*args, **kwargs):
        pass
    guard = SimpleNamespace(page=page, errors=[], rebind=noop, close=noop, timeout_s=.05)
    policy = NativeDocumentPolicy(guard)
    async def body():
        return b"<urlset/>"
    def response(*, frame=None, navigation=True, method="GET", status=200, read=body):
        return SimpleNamespace(url=page.url, status=status, headers={"content-type": "application/xml"}, body=read,
            request=SimpleNamespace(frame=page.main_frame if frame is None else frame, method=method, url=page.url,
                                    is_navigation_request=lambda: navigation))
    def observe(value):
        events.get("request", lambda request: None)(value.request)
        events["response"](value)
        events.get("framenavigated", lambda frame: None)(value.request.frame)
    events["observe"] = observe
    return policy, events, response


async def test_native_bytes_ignore_assets_frames_and_previous_binding():
    policy, events, response = response_fixture()
    events["observe"](response())
    events["observe"](response(navigation=False, status=403))
    events["observe"](response(frame=object(), status=403))
    assert (await policy.read_document("https://catalogue.test/map.xml")).body == b"<urlset/>"
    await policy.rebind("https://catalogue.test/next.xml")
    assert await policy.read_document("https://catalogue.test/next.xml") is None


async def test_native_bytes_never_reuse_post_or_closed_document():
    policy, events, response = response_fixture()
    events["observe"](response())
    events["observe"](response(method="POST"))
    assert await policy.read_document("https://catalogue.test/map.xml") is None
    await policy.close()
    events["observe"](response())
    assert await policy.read_document("https://catalogue.test/map.xml") is None


async def test_native_body_failure_is_bounded_and_sanitized():
    policy, events, response = response_fixture()
    async def failed():
        raise RuntimeError("fixture-secret")
    events["observe"](response(read=failed))
    result = await policy.read_document("https://catalogue.test/map.xml")
    assert result.body == b"" and result.error == "native_body_unavailable"


async def test_native_body_read_preserves_policy_denial_after_await():
    policy, events, response = response_fixture()
    async def denied():
        policy.guard.errors.append(ValueError("destination denied"))
        return b"<urlset/>"
    events["observe"](response(read=denied))
    with pytest.raises(ValueError, match="destination denied"):
        await policy.read_document("https://catalogue.test/map.xml")


async def test_late_response_from_previous_binding_is_not_relabelled():
    policy, events, response = response_fixture()
    previous = response()
    events["observe"](previous)
    await policy.rebind("https://catalogue.test/map.xml")
    events["response"](previous)
    assert await policy.read_document("https://catalogue.test/map.xml") is None


@pytest.mark.parametrize("change", ["rebind", "close", "new_request", "new_url"])
async def test_native_body_read_does_not_outlive_its_document(change):
    policy, events, response = response_fixture()
    async def changing():
        if change == "rebind":
            await policy.rebind("https://catalogue.test/map.xml")
        elif change == "close":
            await policy.close()
        elif change == "new_request":
            events.get("request", lambda request: None)(response().request)
        else:
            policy.guard.page.url = "https://catalogue.test/another.xml"
        return b"<urlset/>"
    events["observe"](response(read=changing))
    result = await policy.read_document("https://catalogue.test/map.xml")
    assert result.body == b"" and result.error == "native_document_changed"


async def test_stalled_native_body_read_has_its_own_timeout():
    policy, events, response = response_fixture()
    async def stalled():
        await asyncio.sleep(.3)
        return b"<urlset/>"
    events["observe"](response(read=stalled))
    result = await policy.read_document("https://catalogue.test/map.xml")
    assert result.body == b"" and result.error == "native_body_unavailable"


async def test_status_only_changes_after_a_main_document_commit():
    policy, events, response = response_fixture()
    events["observe"](response(status=403))
    assert policy.document_status(200) == 403  # a genuine origin denial stays denied
    pending = response(status=200)
    events["request"](pending.request)
    events["response"](pending)
    assert policy.document_status(403) == 403
    events["framenavigated"](policy.guard.page.main_frame)
    assert policy.document_status(403) == 200


async def test_native_body_cancellation_propagates():
    policy, events, response = response_fixture()
    async def cancelled():
        raise asyncio.CancelledError
    events["observe"](response(read=cancelled))
    with pytest.raises(asyncio.CancelledError):
        await policy.read_document("https://catalogue.test/map.xml")
