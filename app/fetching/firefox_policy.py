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
from dataclasses import dataclass
from pathlib import Path

from app.core.netguard import url_is_private
from app.fetching.destinations import DestinationDenied, redirect_destination_allowed
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

    def __init__(self):
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
            config = json.dumps({"origin": self.origin, "token": self.token, "timeoutMs": 5000})
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

    async def _dispatch(self, path, payload):
        if self._closed:
            return {"allow": False}
        if path == "/ready":
            if set(payload) != {"privateAccess"} or type(payload["privateAccess"]) is not bool:
                raise ValueError("invalid readiness")
            if not self._ready.done():
                self._ready.set_result(payload["privateAccess"])
            return {"allow": payload["privateAccess"]}
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
        if (binding is None or binding.store_id != store_id or binding.generation != generation
                or not binding.guard.active or binding.guard.generation != generation):
            return {"allow": False}
        if path == "/bound":
            if set(payload) != {"tabId", "storeId", "generation"}:
                raise ValueError("invalid acknowledgment")
            binding.bound = True
            if not binding.enrolled.done():
                binding.enrolled.set_result(None)
            return {"allow": True, "generation": generation}
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
        self.destination_policy = destination_policy
        self.timeout_s = timeout_s
        self.max_redirects = max_redirects
        self.generation = secrets.token_urlsafe(24)
        self.active = False
        self.errors = []
        self.challenge_post_allowed = bool(challenge_post_allowed)
        self.challenge_form = ChallengeFormAllowance(initial_url, self.challenge_post_allowed)
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
        self._revoke()
        self.initial_url, self.destination_policy = initial_url, destination_policy
        self.generation = secrets.token_urlsafe(24)
        self.errors.clear()
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
