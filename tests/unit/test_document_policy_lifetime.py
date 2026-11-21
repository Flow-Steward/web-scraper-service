"""Dispatcher owns one guard lifetime per page and rebinds sequential fetches."""
import asyncio
from types import SimpleNamespace

from app.fetching.document_policy import bind_document_policy


async def test_native_policy_reuses_binding_and_closes_once(monkeypatch):
    events, starts, bindings, closes = {}, [], [], []
    page = SimpleNamespace(on=lambda name, callback: events.update({name: callback}))
    class Guard:
        errors = []
        def __init__(self, page, initial_url, **kwargs):
            self.page = page
        async def start(self):
            starts.append(self)
        async def rebind(self, url, callback):
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
