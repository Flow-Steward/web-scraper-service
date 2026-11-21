"""Bind native browser document policies with one lifetime per page."""
from __future__ import annotations

import asyncio


class NativeDocumentPolicy:
    def __init__(self, guard):
        self.guard = guard
        self._close_task = None

    @property
    def errors(self):
        return self.guard.errors

    def raise_if_denied(self):
        if self.errors:
            raise self.errors[0]

    async def request(self, route):
        await self.guard.request(route)

    async def rebind(self, initial_url, destination_policy=None):
        if self._close_task is not None:
            raise RuntimeError("native document policy is closed")
        await self.guard.rebind(initial_url, destination_policy)

    def begin_close(self, *_):
        if self._close_task is None:
            self._close_task = asyncio.create_task(self.guard.close())
        return self._close_task

    async def close(self):
        task = self.begin_close()
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise


async def bind_document_policy(page, browser, initial_url, *, destination_policy=None, timeout_s=3, max_redirects=10):
    existing = getattr(page, "_wss_native_document_policy", None)
    if existing is not None:
        await existing.rebind(initial_url, destination_policy)
        return existing
    if browser == "camoufox":
        from app.fetching.firefox_policy import FirefoxDocumentGuard as Guard
    elif browser == "chromium":
        from app.fetching.chromium_destinations import ChromiumDocumentGuard as Guard
    else:
        raise RuntimeError("unsupported native document policy browser")
    guard = Guard(page, initial_url, destination_policy=destination_policy,
                  timeout_s=timeout_s, max_redirects=max_redirects)
    try:
        await guard.start()
    except BaseException:
        await guard.close()
        raise
    policy = NativeDocumentPolicy(guard)
    page._wss_native_document_policy = policy
    page.on("close", policy.begin_close)
    return policy
