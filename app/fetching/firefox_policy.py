"""Native Camoufox document policy via a launch-owned Firefox extension.

The bridge transports verdicts only. Document bytes, redirects, cookies and TLS
remain entirely inside Firefox; no APIRequestContext or HTTP preflight is used.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import re
import secrets
import tempfile
import time
from urllib.parse import urlsplit
from dataclasses import dataclass
from pathlib import Path

from app.core.netguard import url_is_private
from app.fetching.destinations import DestinationDenied, redirect_destination_allowed, document_destination_allowed, resolved
from app.fetching.challenge_forms import ChallengeFormAllowance, MAX_FORM_BYTES


@dataclass
class _Binding:
    guard: object
    generation: str
    enrolled: asyncio.Future
    tab_id: int | None = None
    store_id: str | None = None
    bound: bool = False


class FirefoxPolicyBridge:
    MAX_CONNECTIONS = 32
    MAX_BINDINGS = 128
    MAX_BODY = 16384
    IO_TIMEOUT = 2.0

    def __init__(self, *, table_worker_isolation=False):
        self.table_worker_isolation = table_worker_isolation is True
        self._worker_isolation_ready = False
        self.token = secrets.token_urlsafe(32)
        self.origin = ""
        self.addon_path = ""
        self._server = None
        self._temporary = None
        self._tasks = set()
        self._writers = set()
        self._nonces = {}
        self._tabs = {}
        self._closed = False
        self._close_task = None
        self._table_latches = {}
        self._table_revision = 0
        self._table_ack = 0
        self._table_control_failed = False
        self._table_changed = asyncio.Event()
        self._table_ack_changed = asyncio.Event()
        self._ready = asyncio.get_running_loop().create_future()

    async def start(self):
        try:
            self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0, limit=8192)
            port = self._server.sockets[0].getsockname()[1]
            self.origin = f"http://127.0.0.1:{port}"
            self._temporary = tempfile.TemporaryDirectory(prefix="wss-firefox-policy-")
            self.addon_path = self._temporary.name
            manifest = {
                "manifest_version": 2, "name": "Scraper document policy", "version": "1.0",
                "browser_specific_settings": {"gecko": {"id": "document-policy@flow-steward.invalid"}},
                "incognito": "spanning", "permissions": ["<all_urls>", "tabs", "cookies", "webRequest", "webRequestBlocking"],
                "background": {"scripts": ["background.js"], "persistent": True},
            }
            Path(self.addon_path, "manifest.json").write_text(json.dumps(manifest))
            source = Path(__file__).with_name("firefox_policy_addon.js").read_text()
            config = json.dumps({"origin": self.origin, "token": self.token, "timeoutMs": 5000, "tableWorkerIsolation": self.table_worker_isolation})
            Path(self.addon_path, "background.js").write_text(source.replace("/*BRIDGE_CONFIG*/", config))
            return self
        except BaseException:
            await self.close()
            raise

    async def wait_ready(self, timeout=10):
        private_access = await asyncio.wait_for(asyncio.shield(self._ready), timeout)
        if not private_access:
            raise RuntimeError("Firefox document policy has no private-context access")

    def prepare(self, guard):
        if self._closed or len(self._tabs) + len(self._nonces) >= self.MAX_BINDINGS:
            raise RuntimeError("Firefox document policy unavailable")
        nonce = secrets.token_urlsafe(32)
        future = asyncio.get_running_loop().create_future()
        self._nonces[nonce] = _Binding(guard, guard.generation, future)
        return nonce, future

    def revoke(self, guard):
        for collection in (self._nonces, self._tabs):
            for key, binding in list(collection.items()):
                if binding.guard is guard:
                    collection.pop(key, None)
                    if not binding.enrolled.done():
                        binding.enrolled.cancel()

    def _accept(self, reader, writer):
        if self._closed or len(self._tasks) >= self.MAX_CONNECTIONS:
            writer.close()
            return
        self._writers.add(writer)
        task = asyncio.create_task(self._serve(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _serve(self, reader, writer):
        try:
            async with asyncio.timeout(self.IO_TIMEOUT):
                raw = await reader.readuntil(b"\r\n\r\n")
                lines = raw.decode("ascii").split("\r\n")
                if len(raw) > 8192 or lines[0] not in (
                    "POST /ready HTTP/1.1", "POST /enroll HTTP/1.1", "POST /bound HTTP/1.1", "POST /policy HTTP/1.1",
                    "POST /challenge HTTP/1.1",
                    "POST /form-release HTTP/1.1",
                    "POST /table-control HTTP/1.1", "POST /table-ack HTTP/1.1",
                    "POST /table-policy HTTP/1.1", "POST /table-complete HTTP/1.1",
                    "POST /private-redirect HTTP/1.1",
                ):
                    await self._reply(writer, 400, {"allow": False})
                    return
                headers = {}
                for line in lines[1:-2]:
                    key, value = line.split(":", 1)
                    key = key.strip().lower()
                    if key in headers:
                        raise ValueError("duplicate header")
                    headers[key] = value.strip()
                if not hmac.compare_digest(headers.get("authorization", ""), "Bearer " + self.token):
                    await self._reply(writer, 403, {"allow": False})
                    return
                size = int(headers.get("content-length", "0"))
                if "transfer-encoding" in headers or not 0 < size <= self.MAX_BODY:
                    raise ValueError("invalid body length")
                payload = json.loads(await reader.readexactly(size))
                if not isinstance(payload, dict):
                    raise ValueError("invalid object")
            result = await self._dispatch(lines[0].split()[1], payload)
            await self._reply(writer, 200, result)
        except asyncio.CancelledError:
            raise
        except Exception:
            try:
                await self._reply(writer, 400, {"allow": False})
            except Exception:
                pass
        finally:
            self._writers.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _reply(self, writer, status, result):
        data = json.dumps(result).encode()
        writer.write(f"HTTP/1.1 {status} Result\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\nConnection: close\r\nCache-Control: no-store\r\n\r\n".encode() + data)
        await asyncio.wait_for(writer.drain(), self.IO_TIMEOUT)

    @staticmethod
    def _identity(payload):
        tab_id, store_id = payload.get("tabId"), payload.get("storeId")
        if type(tab_id) is not int or tab_id < 0 or not isinstance(store_id, str) or not 0 < len(store_id) <= 128:
            raise ValueError("invalid tab identity")
        return tab_id, store_id

    async def set_table_mode(self, guard, enabled):
        if enabled:
            binding = next((b for b in self._tabs.values() if b.guard is guard and b.bound
                            and b.generation == guard.generation and guard.active), None)
            if binding is None or self._closed:
                raise RuntimeError("Firefox table binding unavailable")
            self._table_latches[binding.tab_id] = binding
        else:
            self._table_latches = {tab: b for tab, b in self._table_latches.items() if b.guard is not guard}
        self._table_revision += 1
        revision = self._table_revision
        self._table_changed.set()
        async with asyncio.timeout(guard.timeout_s):
            while self._table_ack < revision:
                self._table_ack_changed.clear()
                if self._table_ack < revision:
                    await self._table_ack_changed.wait()
        if enabled and (self._table_control_failed or guard.errors or not guard.active or guard._table_mode != guard.generation or self._tabs.get(binding.tab_id) is not binding):
            raise RuntimeError("Firefox table activation revoked")

    async def _table_control(self, payload):
        if set(payload) != {"after"} or type(payload["after"]) is not int or not 0 <= payload["after"] <= 2**53:
            raise ValueError("invalid table control")
        if payload["after"] == self._table_revision:
            self._table_changed.clear()
            try:
                async with asyncio.timeout(.5):
                    await self._table_changed.wait()
            except TimeoutError:
                pass
        return {"allow": not self._closed, "revision": self._table_revision,
                "tables": [{"tabId": b.tab_id, "storeId": b.store_id, "generation": b.generation}
                           for b in self._table_latches.values()]}

    async def _dispatch(self, path, payload):
        if self._closed:
            return {"allow": False}
        if path == "/ready":
            if (set(payload) not in ({"privateAccess"}, {"privateAccess", "tableWorkerIsolation"})
                    or type(payload["privateAccess"]) is not bool
                    or ("tableWorkerIsolation" in payload and type(payload["tableWorkerIsolation"]) is not bool)):
                raise ValueError("invalid readiness")
            allowed = payload["privateAccess"] and payload.get("tableWorkerIsolation", False) == self.table_worker_isolation
            self._worker_isolation_ready = bool(allowed and self.table_worker_isolation)
            if not self._ready.done():
                self._ready.set_result(allowed)
            return {"allow": allowed, "tableControl": True}
        if path == "/private-redirect":
            if (set(payload) - {"invalid"} not in ({"url"}, {"url", "tabId", "storeId", "generation"})
                    or ("invalid" in payload and type(payload["invalid"]) is not bool)
                    or not isinstance(payload["url"], str) or not 0 < len(payload["url"]) <= 8192):
                raise ValueError("invalid resource redirect")
            binding = None
            if "tabId" in payload:
                tab_id, store_id = self._identity(payload)
                candidate = self._tabs.get(tab_id)
                if (candidate is not None and candidate.bound and candidate.store_id == store_id
                        and candidate.generation == payload.get("generation") and candidate.guard.active
                        and candidate.guard.generation == candidate.generation):
                    binding = candidate
            try:
                async with asyncio.timeout(self.IO_TIMEOUT):
                    parts = urlsplit(payload["url"])
                    allowed = not payload.get("invalid", False) and parts.scheme in ("http", "https") and not await url_is_private(payload["url"])
            except Exception:
                allowed = False
            if (not allowed and binding is not None and self._tabs.get(tab_id) is binding
                    and binding.guard.active and binding.guard.generation == binding.generation
                    and len(binding.guard.errors) < 32):
                binding.guard.errors.append(DestinationDenied(payload["url"], "private_resource_redirect"))
            if allowed and binding is not None:
                from app.fetching.resources import blocks_native_resource

                allowed = not blocks_native_resource(binding.guard, payload["url"])
            return {"allow": allowed}
        if path == "/table-control":
            return await self._table_control(payload)
        if path == "/table-ack":
            if (set(payload) not in ({"revision"}, {"revision", "failed"}) or type(payload["revision"]) is not int
                    or not 0 <= payload["revision"] <= self._table_revision
                    or ("failed" in payload and type(payload["failed"]) is not bool)):
                raise ValueError("invalid table acknowledgment")
            self._table_control_failed = self._table_control_failed or payload.get("failed", False)
            self._table_ack = max(self._table_ack, payload["revision"])
            self._table_ack_changed.set()
            return {"allow": True}
        tab_id, store_id = self._identity(payload)
        if path == "/enroll":
            if set(payload) != {"nonce", "tabId", "storeId"} or not isinstance(payload["nonce"], str):
                raise ValueError("invalid enrollment")
            binding = self._nonces.pop(payload["nonce"], None)
            if binding is None or not binding.guard.active or binding.generation != binding.guard.generation:
                return {"allow": False}
            # Tab IDs cannot claim another live page, even with a fresh nonce.
            previous = self._tabs.get(tab_id)
            if previous is not None and previous.guard is not binding.guard:
                binding.enrolled.cancel()
                return {"allow": False}
            binding.tab_id, binding.store_id = tab_id, store_id
            self._tabs[tab_id] = binding
            return {"allow": True, "generation": binding.generation}
        binding = self._tabs.get(tab_id)
        generation = payload.get("generation")
        retained_completion = False
        if path == "/table-complete":
            retained = self._table_latches.get(tab_id)
            if (retained is not None and retained.bound and retained.store_id == store_id
                    and retained.generation == generation and retained.guard.generation == generation
                    and retained.guard._table_mode == generation and retained.guard.page.is_closed()):
                binding = retained
                retained_completion = True
        if (binding is None or binding.store_id != store_id or binding.generation != generation
                or (not binding.guard.active and not retained_completion) or binding.guard.generation != generation):
            return {"allow": False}
        if path == "/bound":
            if set(payload) != {"tabId", "storeId", "generation"}:
                raise ValueError("invalid acknowledgment")
            binding.bound = True
            if not binding.enrolled.done():
                binding.enrolled.set_result(None)
            return {"allow": True, "generation": generation}
        if path in ("/table-policy", "/table-complete") and binding.bound:
            required = {"tabId", "storeId", "generation", "requestId"}
            request_id = payload.get("requestId")
            if not isinstance(request_id, str) or not 0 < len(request_id) <= 128:
                raise ValueError("invalid table request identity")
            if path == "/table-complete":
                if set(payload) != required | {"failed"} or type(payload["failed"]) is not bool:
                    raise ValueError("invalid table completion")
                binding.guard.table_completed(request_id, payload["failed"])
                return {"allow": True, "generation": generation}
            if set(payload) != required | {"url", "resourceKind", "redirects", "method", "stage"}:
                raise ValueError("invalid table policy")
            url, kind, redirects, method, stage = (payload[k] for k in ("url", "resourceKind", "redirects", "method", "stage"))
            if (not isinstance(url, str) or not 0 < len(url) <= 8192 or not isinstance(kind, str) or not 0 < len(kind) <= 64
                    or type(redirects) is not int or not 0 <= redirects <= 1000
                    or method not in ("GET", "POST", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS", "CONNECT", "TRACE")
                    or stage not in ("request", "redirect")):
                raise ValueError("invalid table policy fields")
            allowed = await binding.guard.decide_table(url, kind, redirects, method, request_id=request_id, stage=stage)
            valid = (self._tabs.get(tab_id) is binding and binding.guard.active and binding.guard.generation == generation)
            return {"allow": bool(allowed and valid), "generation": generation}
        if path == "/challenge" and binding.bound:
            if set(payload) != {"tabId", "storeId", "generation", "url", "requestId", "status", "headers"}:
                raise ValueError("invalid challenge observation")
            url, request_id, status, headers = (payload[k] for k in ("url", "requestId", "status", "headers"))
            if (not isinstance(url, str) or not 0 < len(url) <= 8192
                    or not isinstance(request_id, str) or not 0 < len(request_id) <= 128
                    or type(status) is not int or status != 403 or not isinstance(headers, dict)
                    or set(headers) != {"server", "cf-mitigated", "cf-ray"}
                    or any(value is not None and (not isinstance(value, str) or len(value) > 256) for value in headers.values())):
                raise ValueError("invalid challenge observation fields")
            binding.guard.observe_challenge(url, request_id, status, headers)
            return {"allow": True, "generation": generation}
        if path == "/form-release" and binding.bound:
            if set(payload) != {"tabId", "storeId", "generation", "url", "requestId", "formBytes"}:
                raise ValueError("invalid form release")
            url, request_id, size = (payload[k] for k in ("url", "requestId", "formBytes"))
            if (not isinstance(url, str) or not 0 < len(url) <= 8192
                    or not isinstance(request_id, str) or not 0 < len(request_id) <= 128
                    or type(size) is not int or not 0 < size <= MAX_FORM_BYTES):
                raise ValueError("invalid form release fields")
            allowed = binding.guard.release_form(url, request_id, size)
            return {"allow": allowed, "generation": generation}
        if path != "/policy" or not binding.bound:
            return {"allow": False}
        required = {"tabId", "storeId", "generation", "url", "frameKind", "requestId", "redirects", "method"}
        if set(payload) not in (required, required | {"formBytes"}):
            raise ValueError("invalid policy object")
        url, kind, request_id, redirects, method = (payload[k] for k in ("url", "frameKind", "requestId", "redirects", "method"))
        if (not isinstance(url, str) or not 0 < len(url) <= 8192 or kind not in ("main_frame", "sub_frame")
                or not isinstance(request_id, str) or not 0 < len(request_id) <= 128
                or type(redirects) is not int or not 0 <= redirects <= 1000
                or not isinstance(method, str) or re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,32}", method) is None):
            raise ValueError("invalid policy fields")
        if "formBytes" in payload:
            size = payload["formBytes"]
            if size is not None and (type(size) is not int or not 0 < size <= MAX_FORM_BYTES):
                raise ValueError("invalid form metadata")
            allowed = await binding.guard.decide(url, kind, redirects, method, request_id=request_id, form_bytes=size)
        else:
            allowed = await binding.guard.decide(url, kind, redirects, method)
        # A verdict arriving after revoke/rebind can never release the old document.
        valid = (not self._closed and self._tabs.get(tab_id) is binding and binding.guard.active
                 and binding.guard.generation == generation)
        return {"allow": bool(allowed and valid), "generation": generation}

    def revoke_all(self):
        self._closed = True
        for binding in list(self._tabs.values()) + list(self._nonces.values()):
            binding.guard.active = False
            if not binding.enrolled.done():
                binding.enrolled.cancel()
        self._table_changed.set()
        self._table_ack_changed.set()
        self._tabs.clear()
        self._nonces.clear()

    def begin_close(self):
        self.revoke_all()
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._shutdown())
        return self._close_task

    async def close(self):
        task = self.begin_close()
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def _shutdown(self):
        if not self._ready.done():
            self._ready.cancel()
        if self._server is not None:
            self._server.close()
        for writer in list(self._writers):
            writer.close()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._writers.clear()
        if self._server is not None:
            await self._server.wait_closed()
        if self._temporary is not None:
            self._temporary.cleanup()


class FirefoxDocumentGuard:
    def __init__(self, page, initial_url, *, destination_policy=None, timeout_s=3, max_redirects=10, challenge_post_allowed=False):
        self.page = page
        self.initial_url = initial_url
        self.resource_economy_enabled = False
        self.destination_policy = destination_policy
        self.timeout_s = timeout_s
        self.max_redirects = max_redirects
        self.generation = secrets.token_urlsafe(24)
        self.active = False
        self.errors = []
        self.challenge_post_allowed = bool(challenge_post_allowed)
        self.challenge_form = ChallengeFormAllowance(initial_url, self.challenge_post_allowed)
        self._table_mode = None
        self.table_load_failure = None
        self._table_resource_policy = None
        self._table_pending = {}
        self._table_activity = time.monotonic()
        self.bridge = getattr(page.context.browser, "_wss_firefox_policy_bridge", None)

    async def start(self):
        if self.bridge is None:
            raise RuntimeError("Native Firefox document policy was not installed at launch")
        self.page.on("close", lambda *_: self._revoke())
        await self.rebind(self.initial_url, self.destination_policy)
        return self

    def _revoke(self):
        self.active = False
        self.challenge_form.revoke()
        if self.bridge is not None:
            self.bridge.revoke(self)

    async def rebind(self, initial_url, destination_policy=None, *, challenge_post_allowed=None):
        if self._table_mode is not None:
            raise RuntimeError("end table mode after destroying the old document before rebind")
        self._revoke()
        self.initial_url, self.destination_policy = initial_url, destination_policy
        self.generation = secrets.token_urlsafe(24)
        self.errors.clear()
        self.table_load_failure = None
        if challenge_post_allowed is not None:
            self.challenge_post_allowed = bool(challenge_post_allowed)
        self.challenge_form = ChallengeFormAllowance(initial_url, self.challenge_post_allowed)
        self.active = True
        try:
            nonce, enrolled = self.bridge.prepare(self)
            await self.page.goto("about:blank#wss-document-policy=" + nonce, wait_until="commit", timeout=self.timeout_s * 1000)
            await asyncio.wait_for(asyncio.shield(enrolled), self.timeout_s)
        except BaseException:
            self._revoke()
            raise

    async def enable_table_readonly(self, resource_policy=None):
        if (not self.active or self.bridge is None or not self.bridge.table_worker_isolation or not self.bridge._worker_isolation_ready
                or getattr(self.page, "workers", [])):
            raise RuntimeError("native Firefox table ownership unavailable")
        if self._table_mode == self.generation:
            return
        self._table_mode = self.generation
        self._table_resource_policy = resource_policy
        self.challenge_form.revoke()
        self._table_activity = time.monotonic()
        try:
            await self.bridge.set_table_mode(self, True)
        except BaseException:
            self.active = False  # Remain restrictive; no collector action may run.
            raise

    async def table_quiescent(self, timeout_s, quiet_s=.1):
        generation = self.generation
        started = time.monotonic()
        try:
            async with asyncio.timeout(timeout_s):
                while (self.active or (self._table_mode == generation and self.page.is_closed())) and generation == self.generation:
                    if not self._table_pending and time.monotonic() - max(started, self._table_activity) >= quiet_s:
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
        if self.bridge is not None and not self.bridge._closed:
            if self.page.is_closed():
                self.bridge._table_latches = {tab:b for tab,b in self.bridge._table_latches.items() if b.guard is not self}
                self.bridge._table_revision += 1
                self.bridge._table_changed.set()
            else:
                await self.bridge.set_table_mode(self, False)
        self._table_mode = None
        self._table_resource_policy = None
        self._table_pending.clear()

    def table_completed(self, request_id, failed=False):
        url = self._table_pending.pop(request_id, None)
        self._table_activity = time.monotonic()
        if failed and url is not None and self._table_mode is not None:
            self.table_load_failure = self.table_load_failure or "table_request_failed"

    async def decide_table(self, url, resource_kind, redirects, method, *, request_id, stage="request"):
        generation = self.generation
        try:
            async with asyncio.timeout(self.timeout_s):
                if not self.active or self._table_mode != generation:
                    return False
                if stage == "request":
                    if request_id not in self._table_pending and len(self._table_pending) >= 1024:
                        raise DestinationDenied(url, "table_request_capacity")
                    self._table_pending[request_id] = url
                    self._table_activity = time.monotonic()
                parts = urlsplit(url)
                if (method != "GET" or resource_kind in ("main_frame", "sub_frame") or redirects > self.max_redirects
                        or parts.scheme not in ("http", "https") or parts.username is not None or parts.password is not None):
                    raise DestinationDenied(url, "table_request_denied")
                if await url_is_private(url):
                    from app.fetching.oneoff import PrivateAddress
                    raise PrivateAddress(url)
                if not await document_destination_allowed(url, site_url=self.initial_url, rules={"sensitive_path", "action_params"}):
                    raise DestinationDenied(url)
                if self._table_resource_policy is not None and not await resolved(self._table_resource_policy(url)):
                    raise DestinationDenied(url)
                return self.active and generation == self.generation and self._table_mode == generation
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.active and self.generation == generation and len(self.errors) < 32:
                self.errors.append(exc)
            return False

    def observe_challenge(self, url, request_id, status, headers):
        if self.active:
            self.challenge_form.observe(url, request_id, status, headers)

    def release_form(self, url, request_id, form_bytes):
        return bool(self.active and self.challenge_form.release(url, request_id, form_bytes))

    async def decide(self, url, frame_kind, redirects, method="GET", *, request_id=None, form_bytes=None):
        generation = self.generation
        allowance = self.challenge_form
        reserved = False
        try:
            async with asyncio.timeout(self.timeout_s):
                if not self.active:
                    return False
                if self._table_mode is not None:
                    raise DestinationDenied(url, "table_document_navigation")
                if frame_kind == "main_frame":
                    if method == "GET":
                        allowance.get_started(request_id)
                    elif method == "POST" and redirects == 0:
                        reserved = allowance.reserve(url, request_id, form_bytes)
                        if not reserved:
                            allowance.revoke()
                            raise DestinationDenied(url)
                    else:
                        allowance.revoke()
                        raise DestinationDenied(url)
                if await url_is_private(url):
                    from app.fetching.oneoff import PrivateAddress
                    raise PrivateAddress(url)
                if redirects > self.max_redirects:
                    raise DestinationDenied(url, "too_many_redirects")
                from app.fetching.resources import blocks_native_resource

                if frame_kind == "sub_frame" and blocks_native_resource(self, url):
                    return False
                if (frame_kind == "main_frame" and (reserved or redirects > 0 or url != self.initial_url)
                        and not await redirect_destination_allowed(
                    url, site_url=self.initial_url, destination_policy=self.destination_policy,
                )):
                    raise DestinationDenied(url)
                if reserved and (not self.active or self.generation != generation or not allowance.can_release(request_id)):
                    raise DestinationDenied(url, "guard_revoked")
                return True
        except asyncio.CancelledError:
            if reserved:
                allowance.revoke()
            raise
        except Exception as exc:
            if reserved:
                allowance.revoke()
            if self.active and self.generation == generation and len(self.errors) < 32:
                self.errors.append(exc)
            return False

    def raise_if_denied(self):
        if self.errors:
            raise self.errors[0]

    async def request(self, route):
        # Native addon is the only Firefox document authorizer. Existing route
        # guards still handle private subresources/heavy-resource suppression.
        if not self.active:
            await route.abort()
        else:
            await route.continue_()

    async def close(self):
        self._revoke()
        if not self.page.is_closed():
            await self.page.close()
