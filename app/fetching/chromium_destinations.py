"""Chromium native-transport document redirect guard.

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
    def __init__(self, page, initial_url, *, destination_policy=None, timeout_s=3, max_redirects=10, max_pending=64, max_chains=64):
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
        self.generation = 0
        self.max_pending = max_pending
        self.max_chains = max_chains
        self._close_lock = asyncio.Lock()
        self._cleaned = False
        self._overflow_close = None

    async def start(self):
        try:
            self.session = await self.page.context.new_cdp_session(self.page)
            tree = await self.session.send("Page.getFrameTree")
            self.main_frame_id = tree["frameTree"]["frame"]["id"]
            self.session.on("Fetch.requestPaused", self._on_paused)
            await self.session.send("Fetch.enable", {"patterns": [{
                "resourceType": "Document", "requestStage": "Response",
            }]})
            return self
        except BaseException:
            await self.close()
            raise

    def _remember(self, exc, generation=None):
        if generation is not None and generation != self.generation:
            return
        if len(self.errors) < 100:
            self.errors.append(exc)

    def _on_paused(self, event):
        if self.closed:
            return  # Held in CDP until the already-started page close completes.
        if len(self.tasks) >= self.max_pending:
            self._remember(DestinationDenied(event["request"]["url"], "guard_capacity"))
            self.closed = True
            self.generation += 1
            self._overflow_close = asyncio.create_task(self.close())
            return
        self.paused.add(event["requestId"])
        task = asyncio.create_task(self._response(event, self.generation))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def rebind(self, initial_url, destination_policy=None):
        if self.paused:
            raise RuntimeError("cannot rebind a paused document guard")
        if self.closed:
            raise RuntimeError("document guard is closed")
        self.generation += 1
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

    async def _response(self, event, generation=None):
        generation = self.generation if generation is None else generation
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
                if chain not in self.chains and len(self.chains) >= self.max_chains:
                    raise DestinationDenied(url, "guard_capacity")
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
            if self.closed or generation != self.generation:
                raise DestinationDenied(url, "guard_revoked")
            await self.session.send("Fetch.continueRequest", {"requestId": request_id})
        except Exception as exc:
            self._remember(exc, generation)
            self.chains.pop(event.get("networkId", request_id), None)
            try:
                await self.session.send("Fetch.failRequest", {"requestId": request_id, "errorReason": "BlockedByClient"})
            except Exception:
                pass  # A closed target cannot issue the paused request.
        finally:
            self.paused.discard(request_id)

    async def request(self, route):
        """Use behind private_route_guard; retains native browser request I/O."""
        req = route.request
        generation = self.generation
        try:
            main = req.is_navigation_request() and req.frame == self.page.main_frame
            if main and (req.method != "GET" or (
                req.url != self.initial_url and not await self._allowed(req.url)
            )):
                raise DestinationDenied(req.url)
            if self.closed or generation != self.generation:
                raise DestinationDenied(req.url, "guard_revoked")
            await route.continue_()
        except Exception as exc:
            self._remember(exc, generation)
            await route.abort("blockedbyclient")

    async def close(self):
        # Revoke synchronously before awaiting browser shutdown: a late policy
        # reply must never resume a request from an old lifetime/generation.
        self.closed = True
        self.generation += 1
        async with self._close_lock:
            if self._cleaned:
                return
            try:
                if not self.page.is_closed():
                    await self.page.close()
            finally:
                if self.session is not None:
                    self.session.remove_listener("Fetch.requestPaused", self._on_paused)
                pending = list(self.tasks)
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                self.chains.clear()
                self.paused.clear()
                if self.session is not None:
                    try:
                        await self.session.detach()
                    except Exception:
                        pass
                self._cleaned = True
