"""Experimental Chromium native-transport redirect guard; not enabled by default.

CDP Fetch response interception pauses redirect processing before native follow.
Pair it with the normal request guard for fresh script/meta document requests.
Unlike Route.fetch, this never substitutes APIRequestContext for browser I/O.
https://chromedevtools.github.io/devtools-protocol/tot/Fetch/#event-requestPaused
"""
from __future__ import annotations

import asyncio

from app.core.urls import absolute
from app.fetching.destinations import DestinationDenied, REDIRECT_STATUSES, redirect_destination_allowed
from app.fetching.oneoff import PrivateAddress, url_is_private


class ChromiumDocumentGuard:
    def __init__(self, page, initial_url, *, destination_policy=None, timeout_s=3, max_redirects=10):
        self.page = page
        self.initial_url = initial_url
        self.destination_policy = destination_policy
        self.timeout_s = timeout_s
        self.max_redirects = max_redirects
        self.errors = []
        self.session = None
        self.main_frame_id = None
        self.tasks = set()
        self.paused = set()
        self.chains = {}
        self.closed = False

    async def start(self):
        self.session = await self.page.context.new_cdp_session(self.page)
        tree = await self.session.send("Page.getFrameTree")
        self.main_frame_id = tree["frameTree"]["frame"]["id"]
        self.session.on("Fetch.requestPaused", self._on_paused)
        await self.session.send("Fetch.enable", {"patterns": [{
            "resourceType": "Document", "requestStage": "Response",
        }]})
        return self

    def _on_paused(self, event):
        self.paused.add(event["requestId"])
        task = asyncio.create_task(self._response(event))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def rebind(self, initial_url, destination_policy=None):
        if self.paused:
            raise RuntimeError("cannot rebind a paused document guard")
        self.initial_url = initial_url
        self.destination_policy = destination_policy
        self.errors.clear()
        self.chains.clear()

    def raise_if_denied(self):
        if self.errors:
            raise self.errors[0]

    async def _allowed(self, target, *, private_only=False):
        async with asyncio.timeout(self.timeout_s):
            if await url_is_private(target):
                raise PrivateAddress(target)
            if private_only:
                return True
            return await redirect_destination_allowed(
                target, site_url=self.initial_url, destination_policy=self.destination_policy,
            )

    async def _response(self, event):
        request_id = event["requestId"]
        url = event["request"]["url"]
        try:
            if self.closed:
                raise DestinationDenied(url, "guard_closed")
            headers = {h["name"].lower(): h["value"] for h in event.get("responseHeaders", [])}
            location = headers.get("location")
            network_id = event.get("networkId")
            chain = network_id or request_id
            previous = event.get("redirectedRequestId")
            if event.get("responseStatusCode") in REDIRECT_STATUSES and location:
                count = (self.chains.get(network_id, 0) if network_id else self.chains.pop(previous, 0)) + 1
                self.chains[chain] = count
                if count > self.max_redirects:
                    raise DestinationDenied(url, "too_many_redirects")
                target = absolute(url, location)
                # Third-party challenge frames retain their native networking;
                # every document hop still receives the private-address check.
                private_only = event["frameId"] != self.main_frame_id
                if target is None or not await self._allowed(target, private_only=private_only):
                    raise DestinationDenied(target or url)
            else:
                self.chains.pop(network_id or previous or chain, None)
            await self.session.send("Fetch.continueRequest", {"requestId": request_id})
        except Exception as exc:
            self.errors.append(exc)
            try:
                await self.session.send("Fetch.failRequest", {"requestId": request_id, "errorReason": "BlockedByClient"})
            except Exception:
                pass  # A closed target cannot issue the paused request.
        finally:
            self.paused.discard(request_id)

    async def request(self, route):
        """Use behind private_route_guard; retains native browser request I/O."""
        req = route.request
        try:
            main = req.is_navigation_request() and req.frame == self.page.main_frame
            if main and (req.method != "GET" or (
                req.url != self.initial_url and not await self._allowed(req.url)
            )):
                raise DestinationDenied(req.url)
            await route.continue_()
        except Exception as exc:
            self.errors.append(exc)
            await route.abort("blockedbyclient")

    async def close(self):
        # Never detach a live guard: disabling Fetch can release paused redirects.
        if not self.page.is_closed():
            await self.page.close()
        self.closed = True
        self.session.remove_listener("Fetch.requestPaused", self._on_paused)
        if self.tasks:
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.chains.clear()
        try:
            await self.session.detach()
        except Exception:
            pass
