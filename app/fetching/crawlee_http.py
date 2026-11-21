"""Destination checks around the pinned Crawlee 1.11 Impit document transport.

Keep Crawlee's redirect loop, cookies, origin header stripping, proxy pools and
Impit TLS identity intact. The narrow `_get_client` facade checks each actual
hop before its existing client.request. No HTTP implementation is substituted.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timedelta
from time import monotonic

from crawlee import Request
from crawlee.http_clients import ImpitHttpClient

from app.fetching.destinations import DestinationDenied, redirect_destination_allowed, resolved
from app.fetching.oneoff import Fetched, PrivateAddress, url_is_private


@dataclass
class _Document:
    request: object
    options: dict
    deadline: float | None
    started: bool = False


class _CheckedTransport:
    def __init__(self, client, before_request):
        self.client = client
        self.before_request = before_request

    async def request(self, **kwargs):
        await self.before_request(kwargs["url"])
        return await self.client.request(**kwargs)


class PolicyHttpClient(ImpitHttpClient):
    def __init__(self, *, destination_policy, policy_timeout_s=3.0, **kwargs):
        super().__init__(**kwargs)
        self.destination_policy = destination_policy
        self.policy_timeout_s = policy_timeout_s
        self._document = ContextVar("crawlee_document_destination", default=None)

    async def crawl(self, request, *, timeout=None, **kwargs):
        deadline = monotonic() + timeout.total_seconds() if timeout is not None else None
        token = self._document.set(_Document(request, kwargs, deadline))
        try:
            async with asyncio.timeout(timeout.total_seconds() if timeout is not None else None):
                return await super().crawl(request, timeout=timeout, **kwargs)
        finally:
            self._document.reset(token)

    async def fetch_auxiliary(self, url, *, headers=None):
        """A bounded auxiliary read independent of occupied crawler worker slots.

        Used for uncached redirect-origin robots: reuse this document's Impit
        proxy/session and apply auxiliary per-hop policy without robots recursion.
        """
        current = self._document.get()
        if current is None:
            raise RuntimeError("auxiliary redirect fetch has no document context")
        remaining = current.deadline - monotonic() if current.deadline is not None else 30.0
        if remaining <= 0:
            raise asyncio.TimeoutError
        request = Request.from_url(url, headers=headers, user_data={"aux": True})
        options = {key: value for key, value in current.options.items() if key in {"session", "proxy_info"}}
        result = await self.crawl(request, timeout=timedelta(seconds=min(remaining, 30)), **options)
        response = result.http_response
        return Fetched(url, request.loaded_url or url, response.status_code,
                       (await response.read())[:64 * 1024 * 1024], dict(response.headers))

    def _get_client(self, proxy_url):
        # Pinned Crawlee 1.11.2 seam. Its documented public HttpClient.crawl is
        # retained; every redirect still passes this existing Impit client call.
        return _CheckedTransport(super()._get_client(proxy_url), self._before_document_hop)

    async def _before_document_hop(self, target):
        document = self._document.get()
        if document is None:
            return
        request = document.request
        try:
            async with asyncio.timeout(self.policy_timeout_s):
                if await url_is_private(target):
                    raise PrivateAddress(target)
                if document.started:
                    async def allowed(url):
                        return await resolved(self.destination_policy(request, url))
                    if not await redirect_destination_allowed(
                        target, site_url=request.url, destination_policy=allowed,
                    ):
                        raise DestinationDenied(target)
                document.started = True
        except Exception:
            request.no_retry = True
            raise

    async def send_request(self, *args, **kwargs):
        # JSON/resource requests retain their caller-owned admission semantics.
        token = self._document.set(None)
        try:
            return await super().send_request(*args, **kwargs)
        finally:
            self._document.reset(token)

    @asynccontextmanager
    async def stream(self, *args, **kwargs):
        token = self._document.set(None)
        try:
            async with super().stream(*args, **kwargs) as response:
                yield response
        finally:
            self._document.reset(token)
