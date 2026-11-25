"""Bind native browser document policies with one lifetime per page."""
from __future__ import annotations

import asyncio
import secrets


class TableLeaseError(RuntimeError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class NativeTableLease:
    """An exclusive native read-only binding retained until the old document dies."""

    def __init__(self, policy):
        self.policy = policy
        self.guard = policy.guard
        self.page = self.guard.page
        self.context = self.page.context
        self.browser = self.context.browser
        self.errors = []
        self.released = False
        self.retiring = False
        self.reset_url = None
        self._page_route = False
        self._context_route = False
        self._native_enabled = False
        self._tasks = set()
        self._listeners = []
        self._retire_lock = asyncio.Lock()

    def _denied(self, reason):
        if not self.errors:
            self.errors.append(TableLeaseError(reason))

    @property
    def load_failure(self):
        return getattr(self.guard, "table_load_failure", None)

    @property
    def stop_reason(self):
        return self.errors[0].reason if self.errors else None

    def _spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _born_page(self, page):
        if page is not self.page:
            self._denied("popup_navigation")
            self._spawn(page.close())

    def _born_worker(self, *_):
        self._denied("unsupported_worker")
        self._spawn(self.context.close())

    async def _gate(self, route):
        request = route.request
        try:
            owner = request.frame.page
        except Exception:
            owner = None
        if owner is not self.page:
            self._denied("popup_navigation" if request.is_navigation_request() else "unowned_request")
            await route.abort("blockedbyclient")
            return
        if request.method != "GET":
            self._denied("unsupported_method")
            await route.abort("blockedbyclient")
            return
        if request.is_navigation_request() and request.frame == self.page.main_frame:
            if self.reset_url is not None and request.url == self.reset_url:
                await route.fallback()
                return
            self._denied("document_navigation")
            await route.fulfill(status=204, body="")
            return
        if self.errors:
            await route.abort("blockedbyclient")
            return
        await route.fallback()

    async def activate(self, resource_policy=None):
        try:
            exclusive = (getattr(self.context, "_wss_table_owned", False) is True
                         and self.browser is not None and self.browser.contexts == [self.context]
                         and self.context.pages == [self.page] and not self.page.workers
                         and not self.context.service_workers
                         and (getattr(self.guard, "bridge", None) is None
                              or getattr(self.browser, "_wss_table_worker_isolation", False) is True
                              and getattr(self.guard.bridge, "table_worker_isolation", False) is True
                              and getattr(self.guard.bridge, "_worker_isolation_ready", False) is True))
        except Exception:
            exclusive = False
        if not exclusive:
            raise TableLeaseError("unsupported_context")
        if not all(callable(getattr(self.guard, name, None)) for name in
                   ("enable_table_readonly", "table_quiescent", "end_table_readonly")):
            raise TableLeaseError("unsupported_browser")
        try:
            for surface, event, handler in ((self.context, "page", self._born_page),
                                            (self.context, "serviceworker", self._born_worker),
                                            (self.page, "worker", self._born_worker)):
                surface.on(event, handler)
                self._listeners.append((surface, event, handler))
            await self.context.route("**/*", self._gate)
            self._context_route = True
            await self.page.route("**/*", self._gate)
            self._page_route = True
            blocked_workers = await self.page.evaluate("""() => {
                if (!document.head) return false;
                const policy = document.createElement('meta');
                policy.httpEquiv = 'Content-Security-Policy';
                policy.content = "worker-src 'none'";
                document.head.appendChild(policy);
                return true;
            }""")
            if not blocked_workers:
                raise TableLeaseError("unsupported_context")
            # Treat partial activation as potentially armed; teardown may only
            # revoke it after the exclusive old transport has been destroyed.
            self._native_enabled = True
            acknowledged = await self.guard.enable_table_readonly(resource_policy=resource_policy)
            if acknowledged is False:
                raise TableLeaseError("unsupported_browser")
            if self.context.pages != [self.page] or self.page.workers or self.context.service_workers:
                raise TableLeaseError("unsupported_context")
            self.raise_if_denied()
        except BaseException:
            try:
                await self.context.close()
            except BaseException:
                await self.browser.close()
            await self.retire(close_page=True)
            raise

    def raise_if_denied(self):
        if self.released or self.retiring:
            raise TableLeaseError("lease_closed")
        self.policy._raise_native_denial()
        if self.errors:
            raise self.errors[0]

    async def quiescent(self, timeout_s, quiet_s=.1):
        self.raise_if_denied()
        quiet = await self.guard.table_quiescent(timeout_s, quiet_s=quiet_s)
        self.raise_if_denied()
        return bool(quiet)

    async def _close_popups(self):
        for page in list(self.context.pages):
            if page is not self.page and not page.is_closed():
                await page.close()
        tasks = list(self._tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def retire(self, *, close_page=False):
        async with self._retire_lock:
            if self.released:
                return
            self.retiring = True
            failure = None
            destroyed = self.page.is_closed()
            context_closed = False
            try:
                if not destroyed:
                    if close_page:
                        await self.page.close()
                    else:
                        self.reset_url = "about:blank#wss-table-retire=" + secrets.token_urlsafe(32)
                        await self.page.goto(self.reset_url, wait_until="commit", timeout=max(1, self.guard.timeout_s * 1000))
                        if self.page.url != self.reset_url:
                            raise TableLeaseError("lease_reset_failed")
                    destroyed = True
                await self._close_popups()
                if self._native_enabled:
                    quiet = await self.guard.table_quiescent(self.guard.timeout_s, quiet_s=.05)
                    if not quiet:
                        # Keepalive chains can outlive a page. Destroy the proved
                        # exclusive transport before any native read latch ends.
                        await self.context.close()
                        context_closed = True
                        if not close_page:
                            failure = TableLeaseError("lease_reset_failed")
            except BaseException as exc:
                failure = exc
                # Cancellation/failure may never release a live old document.
                try:
                    await self.context.close()
                    context_closed = True
                    destroyed = True
                except BaseException:
                    await self.browser.close()
                    context_closed = True
                    destroyed = True
            finally:
                if destroyed:
                    try:
                        if self._native_enabled:
                            await self.guard.end_table_readonly()
                    finally:
                        for surface, installed in ((self.page, self._page_route), (self.context, self._context_route)):
                            if installed:
                                try:
                                    await surface.unroute("**/*", self._gate)
                                except Exception:
                                    pass  # Closed transports have no live requests.
                        for surface, event, handler in self._listeners:
                            try:
                                surface.remove_listener(event, handler)
                            except Exception:
                                pass
                        self._listeners.clear()
                        self.released = True
            if failure is not None:
                raise failure
            if context_closed and not close_page:
                raise TableLeaseError("lease_reset_failed")


class NativeDocumentPolicy:
    def __init__(self, guard):
        self.guard = guard
        self._close_task = None
        self._table_lease = None
        self._table_lock = asyncio.Lock()
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
        return [*self.guard.errors, *(self._table_lease.errors if self._table_lease is not None else [])]

    def _raise_native_denial(self):
        if self.guard.errors:
            raise self.guard.errors[0]

    def raise_if_denied(self):
        self._raise_native_denial()
        if self._table_lease is not None and self._table_lease.errors:
            raise self._table_lease.errors[0]

    async def enable_table_readonly(self, resource_policy=None):
        async with self._table_lock:
            if self._close_task is not None:
                raise TableLeaseError("lease_closed")
            if self._table_lease is not None:
                self._table_lease.raise_if_denied()
                return self._table_lease
            lease = NativeTableLease(self)
            self._table_lease = lease  # Close events must register the barrier even during activation.
            try:
                await lease.activate(resource_policy)
            except BaseException:
                if not lease._page_route and not lease._context_route:
                    self._table_lease = None
                raise
            return lease

    async def request(self, route):
        await self.guard.request(route)

    async def rebind(self, initial_url, destination_policy=None, **kwargs):
        if self._close_task is not None:
            raise RuntimeError("native document policy is closed")
        if self._table_lease is not None:
            self.raise_if_denied()
            await self._table_lease.retire()
            self.raise_if_denied()  # Retirement may expose a final forbidden old-document request.
            self._table_lease = None
        self._epoch += 1
        self.response, self._request, self._request_epoch, self._committed_request = None, None, None, None
        await self.guard.rebind(initial_url, destination_policy, **kwargs)

    async def _close_native(self):
        try:
            if self._table_lease is not None:
                await self._table_lease.retire(close_page=True)
        finally:
            await self.guard.close()

    def begin_close(self, *_):
        self._epoch += 1
        self.response, self._request, self._request_epoch, self._committed_request = None, None, None, None
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_native())
            if self._table_lease is not None:
                self.guard.page.context._wss_table_cleanup_task = self._close_task
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
