"""Bind native browser document policies with one lifetime per page."""
from __future__ import annotations

import asyncio


class NativeDocumentPolicy:
    def __init__(self, guard):
        self.guard = guard
        self._close_task = None
        self.response = None
        self._epoch = 0
        self._request = None
        self._request_epoch = None
        self._committed_request = None
        guard.page.on("request", self._observe_request)
        guard.page.on("response", self._observe_response)
        guard.page.on("framenavigated", self._observe_commit)

    def _observe_request(self, request):
        if self._close_task is not None:
            return
        try:
            if request.is_navigation_request() and request.frame == self.guard.page.main_frame:
                self._request, self._request_epoch = request, self._epoch
                self._committed_request = None
                self.response = None
        except Exception:
            pass

    def _observe_response(self, response):
        if self._close_task is not None:
            return
        request = response.request
        try:
            if request is self._request and self._request_epoch == self._epoch:
                # Keep one native main-document response, including a clearance
                # reload. Assets and child frames cannot replace its bytes.
                self.response = response if request.method == "GET" else None
        except Exception:
            pass  # A detached frame has no usable document response.

    def _observe_commit(self, frame):
        from urllib.parse import urldefrag

        if self._close_task is not None or frame != self.guard.page.main_frame:
            return
        request = self._request
        self._committed_request = (
            request if request is not None and urldefrag(request.url)[0] == urldefrag(frame.url)[0] else None
        )

    def _current(self, response, epoch):
        from urllib.parse import urldefrag

        return (self._close_task is None and epoch == self._epoch and response is not None
                and response is self.response and response.request is self._request
                and self._committed_request is self._request
                and self._request_epoch == epoch
                and urldefrag(response.url)[0] == urldefrag(self.guard.page.url)[0])

    def document_status(self, fallback):
        return self.response.status if self._current(self.response, self._epoch) else fallback

    async def read_document(self, requested_url):
        from app.fetching.oneoff import Fetched, MAX_DECOMPRESSED

        response, epoch = self.response, self._epoch
        if not self._current(response, epoch):
            return None
        self.raise_if_denied()
        try:
            async with asyncio.timeout(self.guard.timeout_s):
                body = await response.body()
        except Exception:
            self.raise_if_denied()
            return Fetched(requested_url, response.url, response.status, b"", {}, "native_body_unavailable")
        self.raise_if_denied()
        if not self._current(response, epoch):
            return Fetched(requested_url, response.url, response.status, b"", {}, "native_document_changed")
        return Fetched(requested_url, response.url, response.status, body[:MAX_DECOMPRESSED], dict(response.headers))

    @property
    def errors(self):
        return self.guard.errors

    def raise_if_denied(self):
        if self.errors:
            raise self.errors[0]

    async def request(self, route):
        await self.guard.request(route)

    async def rebind(self, initial_url, destination_policy=None, **kwargs):
        if self._close_task is not None:
            raise RuntimeError("native document policy is closed")
        self._epoch += 1
        self.response, self._request, self._request_epoch, self._committed_request = None, None, None, None
        await self.guard.rebind(initial_url, destination_policy, **kwargs)

    def begin_close(self, *_):
        self._epoch += 1
        self.response, self._request, self._request_epoch, self._committed_request = None, None, None, None
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


async def bind_document_policy(page, browser, initial_url, *, destination_policy=None, timeout_s=3, max_redirects=10,
                               proxy_auth=None, on_proxy_auth_required=None, challenge_post_allowed=False):
    auth_options = {"proxy_auth": proxy_auth, "on_proxy_auth_required": on_proxy_auth_required} if browser == "chromium" else {}
    if browser == "camoufox":
        auth_options["challenge_post_allowed"] = bool(challenge_post_allowed)
    existing = getattr(page, "_wss_native_document_policy", None)
    if existing is not None:
        await existing.rebind(initial_url, destination_policy, **auth_options)
        return existing
    if browser == "camoufox":
        from app.fetching.firefox_policy import FirefoxDocumentGuard as Guard
    elif browser == "chromium":
        from app.fetching.chromium_destinations import ChromiumDocumentGuard as Guard
    else:
        raise RuntimeError("unsupported native document policy browser")
    guard = Guard(page, initial_url, destination_policy=destination_policy,
                  timeout_s=timeout_s, max_redirects=max_redirects, **auth_options)
    try:
        await guard.start()
    except BaseException:
        await guard.close()
        raise
    policy = NativeDocumentPolicy(guard)
    page._wss_native_document_policy = policy
    page.on("close", policy.begin_close)
    return policy
