"""Chromium native-transport document redirect guard.

CDP Fetch response interception pauses redirect processing before native follow.
Pair it with the normal request guard for fresh script/meta document requests.
Unlike Route.fetch, this never substitutes APIRequestContext for browser I/O.
https://chromedevtools.github.io/devtools-protocol/tot/Fetch/#event-requestPaused
"""
from __future__ import annotations

import asyncio
import time
from urllib.parse import urlsplit

from app.fetching.native_redirects import native_redirect_target
from app.fetching.destinations import DestinationDenied, REDIRECT_STATUSES, redirect_destination_allowed, document_destination_allowed, resolved
from app.fetching.oneoff import PrivateAddress, url_is_private
from app.fetching.chromium_targets import ChildTargets, FETCH_OPTIONS, TABLE_FETCH_OPTIONS
from app.fetching.chromium_resources import BrowserResourcePrivacy
from app.fetching.proxy_auth import ProxyAuthenticationRequired


class ChromiumDocumentGuard:
    def __init__(self, page, initial_url, *, destination_policy=None, timeout_s=3, max_redirects=10, max_pending=64, max_chains=64, proxy_auth=None, on_proxy_auth_required=None):
        self.page = page
        self.initial_url = initial_url
        self.resource_economy_enabled = False
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
        self.child_targets = None
        self._proxy_auth = dict(proxy_auth) if proxy_auth is not None else None
        self._on_proxy_auth_required = on_proxy_auth_required
        self._proxy_auth_rejected = False
        self.auth_challenges = {}
        self.resource_privacy = None
        self._table_mode = None
        self.table_load_failure = None
        self._table_resource_policy = None
        self._network_pending = {}
        self._network_overflow = False
        self._network_activity = time.monotonic()

    async def start(self):
        try:
            self.resource_privacy = await BrowserResourcePrivacy.for_browser(self.page.context.browser)
            self.session = await self.page.context.new_cdp_session(self.page)
            tree = await self.session.send("Page.getFrameTree")
            self.main_frame_id = tree["frameTree"]["frame"]["id"]
            self.session.on("Fetch.requestPaused", self._on_paused)
            self.session.on("Fetch.authRequired", self._on_auth_required)
            self.session.on("Network.requestWillBeSent", self._on_network_request)
            self.session.on("Network.loadingFinished", self._on_network_done)
            self.session.on("Network.loadingFailed", self._on_network_failed)
            await self.session.send("Network.enable")
            await self.session.send("Fetch.enable", FETCH_OPTIONS)
            self.child_targets = ChildTargets(
                self.session, on_paused=self._on_paused, on_failure=self._child_failure,
                spawn=self._spawn, is_closed=lambda: self.closed,
                timeout_s=self.timeout_s, max_children=self.max_chains,
                on_detached=self._child_detached, on_auth_required=self._on_auth_required,
                fetch_options=lambda: TABLE_FETCH_OPTIONS if self._table_mode is not None else FETCH_OPTIONS,
                table_active=lambda: self._table_mode is not None, on_network=self._on_network_event,
            )
            await self.child_targets.start()
            self.resource_privacy.register(self)
            return self
        except BaseException:
            await self.close()
            raise

    def _remember(self, exc, generation=None):
        if generation is not None and generation != self.generation:
            return
        if isinstance(exc, ProxyAuthenticationRequired) and self._proxy_auth is not None:
            self._proxy_rejected(self.generation if generation is None else generation)
        elif len(self.errors) < 100:
            self.errors.append(exc)

    def _child_detached(self, channel):
        namespace = id(channel)
        self.chains = {key: count for key, count in self.chains.items() if key[0] != namespace}
        self.paused = {key for key in self.paused if key[0] != namespace}
        self.auth_challenges = {key: count for key, count in self.auth_challenges.items() if key[0] != namespace}
        self._network_pending = {key: value for key, value in self._network_pending.items() if key[0] != namespace}
        self._network_activity = time.monotonic()

    def _child_failure(self, reason):
        if self.closed:
            return
        self._remember(DestinationDenied(self.initial_url, reason))
        self.closed = True
        self.generation += 1
        self._overflow_close = asyncio.create_task(self.close())

    def _spawn(self, coroutine):
        if self.closed or len(self.tasks) >= self.max_pending:
            coroutine.close()
            self._child_failure("guard_capacity")
            return None
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def _on_network_event(self, method, event, channel):
        callback = {"Network.requestWillBeSent": self._on_network_request,
                    "Network.loadingFinished": self._on_network_done,
                    "Network.loadingFailed": self._on_network_failed}.get(method)
        if callback is not None:
            callback(event, channel)

    def _on_network_request(self, event, channel=None):
        channel = self.session if channel is None else channel
        key = (id(channel), event["requestId"])
        if key not in self._network_pending and len(self._network_pending) >= 1024:
            self._network_overflow = True
            if self._table_mode is not None:
                self._child_failure("table_request_capacity")
            return
        self._network_pending[key] = (event.get("request", {}).get("url", self.initial_url), event.get("type"))
        self._network_activity = time.monotonic()

    def _on_network_done(self, event, channel=None):
        channel = self.session if channel is None else channel
        self._network_pending.pop((id(channel), event["requestId"]), None)
        self._network_activity = time.monotonic()

    def _on_network_failed(self, event, channel=None):
        channel = self.session if channel is None else channel
        pending = self._network_pending.get((id(channel), event["requestId"]))
        if self._table_mode is not None and pending is not None and pending[1] in ("XHR", "Fetch") and not self.closed:
            self.table_load_failure = self.table_load_failure or "table_request_failed"
        self._on_network_done(event, channel)

    async def enable_table_readonly(self, resource_policy=None):
        if self.closed or self._network_overflow or getattr(self.page, "workers", []):
            raise RuntimeError("native table ownership unavailable")
        if self._table_mode == self.generation:
            return
        self._table_mode = self.generation
        self._table_resource_policy = resource_policy
        self._network_activity = time.monotonic()
        try:
            async with asyncio.timeout(self.timeout_s):
                if self.resource_privacy is not None:
                    await self.resource_privacy.enable_table(self)
                await self.session.send("Fetch.enable", TABLE_FETCH_OPTIONS)
                if self.child_targets is not None:
                    await self.child_targets.enable_table()
            if self.closed or self._table_mode != self.generation:
                raise RuntimeError("native table binding revoked")
        except BaseException:
            # Never drop restrictive mode after partial activation.
            await self.close()
            raise

    async def table_quiescent(self, timeout_s, quiet_s=.1):
        generation = self.generation
        started = time.monotonic()
        try:
            async with asyncio.timeout(timeout_s):
                while not self.closed and generation == self.generation:
                    if not self._network_pending and not self.paused and (self.resource_privacy is None or (not self.resource_privacy.paused and not self.resource_privacy.tasks)) and time.monotonic() - max(started, self._network_activity) >= quiet_s:
                        return True
                    await asyncio.sleep(.01)
        except TimeoutError:
            pass
        return False

    async def end_table_readonly(self):
        if self._table_mode is None:
            return
        if not self.page.is_closed() and not self.page.url.startswith("about:blank"):
            raise RuntimeError("destroy the table document before ending its guard")
        if not self.page.is_closed():
            async with asyncio.timeout(self.timeout_s):
                await self.session.send("Fetch.enable", FETCH_OPTIONS)
                if self.child_targets is not None:
                    await self.child_targets.end_table()
        if self.resource_privacy is not None:
            await self.resource_privacy.end_table(self)
        self._table_mode = None
        self._table_resource_policy = None
        self._network_pending.clear()

    async def _table_allowed(self, url, method):
        async with asyncio.timeout(self.timeout_s):
            parts = urlsplit(url)
            if method != "GET" or parts.scheme not in ("http", "https") or parts.username is not None or parts.password is not None:
                return False
            if await url_is_private(url):
                raise PrivateAddress(url)
            if not await document_destination_allowed(url, site_url=self.initial_url, rules={"sensitive_path", "action_params"}):
                return False
            return self._table_resource_policy is None or bool(await resolved(self._table_resource_policy(url)))

    def _on_paused(self, event, channel=None):
        if self.closed:
            return  # Held until the already-started page close completes.
        channel = self.session if channel is None else channel
        key = (id(channel), event["requestId"])
        self.paused.add(key)
        if self._spawn(self._response(event, self.generation, channel)) is None:
            self.paused.discard(key)

    def _on_auth_required(self, event, channel=None):
        if self.closed:
            return
        channel = self.session if channel is None else channel
        key = (id(channel), event["requestId"])
        self.paused.add(key)
        if self._spawn(self._auth(event, self.generation, channel)) is None:
            self.paused.discard(key)

    def _proxy_rejected(self, generation):
        if self.closed or generation != self.generation or self._proxy_auth is None or self._proxy_auth_rejected:
            return
        self._proxy_auth_rejected = True
        if len(self.errors) < 100:
            self.errors.append(ProxyAuthenticationRequired())
        if self._on_proxy_auth_required is not None:
            self._on_proxy_auth_required()

    async def _auth(self, event, generation=None, channel=None):
        generation = self.generation if generation is None else generation
        channel = self.session if channel is None else channel
        key = (id(channel), event["requestId"])
        try:
            if self.closed or generation != self.generation or getattr(channel, "closed", False):
                return
            source = event.get("authChallenge", {}).get("source")
            auth_key = (*key, source)
            count = self.auth_challenges.get(auth_key, 0)
            if count >= 2 or (auth_key not in self.auth_challenges and len(self.auth_challenges) >= self.max_chains):
                self._child_failure("auth_challenge_capacity")
                return
            self.auth_challenges[auth_key] = count + 1
            response = {"response": "Default"}
            # Proxy credentials must NEVER answer an origin/Server challenge.
            if source == "Proxy":
                credentials = self._proxy_auth or {}
                if count == 0 and credentials.get("username") is not None and credentials.get("password") is not None:
                    response = {"response": "ProvideCredentials", "username": credentials["username"], "password": credentials["password"]}
                else:
                    response = {"response": "CancelAuth"}
                    self._proxy_rejected(generation)
            if self.closed or generation != self.generation:
                return
            async with asyncio.timeout(self.timeout_s):
                await channel.send("Fetch.continueWithAuth", {"requestId": event["requestId"], "authChallengeResponse": response})
        except Exception:
            # CDP errors must not retain the credential-bearing command payload.
            if not self.closed and generation == self.generation and not getattr(channel, "closed", False):
                self._child_failure("auth_command_failed")
        finally:
            self.paused.discard(key)

    async def rebind(self, initial_url, destination_policy=None, *, proxy_auth=None, on_proxy_auth_required=None):
        if self._table_mode is not None:
            raise RuntimeError("end table mode after destroying the old document before rebind")
        if self.paused:
            raise RuntimeError("cannot rebind a paused document guard")
        if self.closed:
            raise RuntimeError("document guard is closed")
        self.generation += 1
        self.initial_url = initial_url
        self.destination_policy = destination_policy
        self.errors.clear()
        self.table_load_failure = None
        self.chains.clear()
        self.auth_challenges.clear()
        self._proxy_auth = dict(proxy_auth) if proxy_auth is not None else None
        self._on_proxy_auth_required = on_proxy_auth_required
        self._proxy_auth_rejected = False

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

    async def _response(self, event, generation=None, channel=None):
        generation = self.generation if generation is None else generation
        channel = self.session if channel is None else channel
        request_id = event["requestId"]
        namespace = id(channel)
        key = (namespace, request_id)
        url = event["request"]["url"]
        try:
            if self.closed or generation != self.generation:
                raise DestinationDenied(url, "guard_revoked")
            if self._table_mode is not None:
                if self._table_mode != generation or not await self._table_allowed(url, event["request"].get("method", "GET")):
                    raise DestinationDenied(url, "table_request_denied")
                if event.get("resourceType") == "Document":
                    raise DestinationDenied(url, "table_document_navigation")
            if "responseStatusCode" not in event and "responseErrorReason" not in event:
                # Request-stage auth enrollment is not a completed response. In
                # particular it must not erase the preceding redirect count.
                await channel.send("Fetch.continueRequest", {"requestId": request_id})
                return
            if event.get("responseStatusCode") == 407 and self._proxy_auth is not None:
                self._proxy_rejected(generation)
                raise ProxyAuthenticationRequired()
            if self._table_mode is not None and event.get("resourceType") in ("XHR", "Fetch") and event.get("responseStatusCode", 0) >= 400:
                self.table_load_failure = self.table_load_failure or "table_http_failed"
            headers = {h["name"].lower(): h["value"] for h in event.get("responseHeaders", [])}
            locations = [h["value"] for h in event.get("responseHeaders", []) if h["name"].lower() == "location"]
            if event.get("responseStatusCode") in REDIRECT_STATUSES and len(locations) > 1:
                raise DestinationDenied(url, "ambiguous_redirect")
            location = locations[0] if locations else None
            network_id = (namespace, event["networkId"]) if event.get("networkId") else None
            chain = network_id or key
            previous = (namespace, event["redirectedRequestId"]) if event.get("redirectedRequestId") else None
            if event.get("responseStatusCode") in REDIRECT_STATUSES and location:
                count = (self.chains.get(network_id, 0) if network_id else self.chains.pop(previous, 0)) + 1
                if chain not in self.chains and len(self.chains) >= self.max_chains:
                    raise DestinationDenied(url, "guard_capacity")
                self.chains[chain] = count
                if count > self.max_redirects:
                    raise DestinationDenied(url, "too_many_redirects")
                target = native_redirect_target(url, location)
                # Third-party challenge frames retain their native networking;
                # every document hop still receives the private-address check.
                private_only = event.get("resourceType", "Document") != "Document" or channel is not self.session or event["frameId"] != self.main_frame_id
                allowed = (target is not None and (await self._table_allowed(target, event["request"].get("method", "GET"))
                           if self._table_mode is not None else await self._allowed(target, private_only=private_only)))
                if not allowed:
                    raise DestinationDenied(target or url)
                from app.fetching.resources import blocks_native_resource

                if private_only and blocks_native_resource(self, target):
                    self.chains.pop(chain, None)
                    await channel.send("Fetch.failRequest", {"requestId": request_id, "errorReason": "BlockedByClient"})
                    return  # Optional-resource suppression is not a document failure.
            else:
                self.chains.pop(network_id or previous or chain, None)
            if self.closed or generation != self.generation:
                raise DestinationDenied(url, "guard_revoked")
            await channel.send("Fetch.continueRequest", {"requestId": request_id})
        except Exception as exc:
            if not getattr(channel, "closed", False):
                self._remember(exc, generation)
            self.chains.pop((namespace, event.get("networkId", request_id)), None)
            try:
                await channel.send("Fetch.failRequest", {"requestId": request_id, "errorReason": "BlockedByClient"})
            except Exception:
                pass  # A closed target cannot issue the paused request.
        finally:
            self.paused.discard(key)

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
                if self.child_targets is not None:
                    self.child_targets.close()
                if self.session is not None:
                    self.session.remove_listener("Fetch.requestPaused", self._on_paused)
                    self.session.remove_listener("Fetch.authRequired", self._on_auth_required)
                    self.session.remove_listener("Network.requestWillBeSent", self._on_network_request)
                    self.session.remove_listener("Network.loadingFinished", self._on_network_done)
                    self.session.remove_listener("Network.loadingFailed", self._on_network_failed)
                pending = list(self.tasks)
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                if self.resource_privacy is not None:
                    try:
                        await self.resource_privacy.end_table(self)
                    except Exception:
                        pass  # Browser teardown already revokes every target.
                    self.resource_privacy.unregister(self)
                self._network_pending.clear()
                self.chains.clear()
                self.paused.clear()
                self.auth_challenges.clear()
                self._proxy_auth = None
                self._on_proxy_auth_required = None
                if self.session is not None:
                    try:
                        await self.session.detach()
                    except Exception:
                        pass
                self._cleaned = True
