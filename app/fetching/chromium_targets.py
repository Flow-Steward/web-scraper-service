"""Bounded public-CDP child channels for native out-of-process iframe guards.

Playwright's public CDPSession.send has no flat-session envelope argument. Use
CDP's supported non-flat Target messages, with a per-session debugger-start
throttle: Fetch interception and recursive attachment precede script release.
https://chromedevtools.github.io/devtools-protocol/tot/Target/#method-setAutoAttach
"""
from __future__ import annotations

import asyncio
import json

_AUTO_ATTACH = {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": False,
                "filter": [{"type": "iframe", "exclude": False}, {"exclude": True}]}
# Response-only interception suppresses Chromium's native proxy credential retry.
# Request interception enables authRequired while response interception still gates redirects.
FETCH_OPTIONS = {"handleAuthRequests": True, "patterns": [
    {"resourceType": "Document", "requestStage": "Request"},
    {"resourceType": "Document", "requestStage": "Response"},
]}


class _ChildChannel:
    is_child = True

    def __init__(self, owner, parent, session_id):
        self.owner, self.parent, self.session_id = owner, parent, session_id
        self.closed, self.sequence = False, 0
        self.initialized = False
        self.pending = {}

    async def send(self, method, params=None):
        owner = self.owner
        if self.closed or owner.closed or owner.is_closed():
            raise RuntimeError("child document policy is closed")
        if owner.pending_commands >= owner.max_commands:
            owner.fail("child_command_capacity")
            raise RuntimeError("child document policy capacity reached")
        self.sequence += 1
        command_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[command_id] = future
        owner.pending_commands += 1
        try:
            async with asyncio.timeout(owner.timeout_s):
                await self.parent.send("Target.sendMessageToTarget", {
                    "sessionId": self.session_id,
                    "message": json.dumps({"id": command_id, "method": method, "params": params or {}}),
                })
                return await future
        except asyncio.CancelledError:
            raise
        except Exception:
            if self.initialized and not self.closed and not owner.closed and not owner.is_closed():
                owner.fail("child_target_command_failed")
            raise
        finally:
            self.pending.pop(command_id, None)
            owner.pending_commands -= 1
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()  # Consume a detach error during parent.send.

    def close(self):
        self.closed = True
        for future in self.pending.values():
            if not future.done():
                future.set_exception(RuntimeError("child target detached"))


class ChildTargets:
    def __init__(self, root, *, on_paused, on_failure, spawn, is_closed,
                 timeout_s=3, max_children=64, max_commands=128, on_detached=None, on_auth_required=None):
        self.root, self.on_paused, self.on_failure = root, on_paused, on_failure
        self.spawn, self.is_closed, self.timeout_s = spawn, is_closed, timeout_s
        self.max_children, self.max_commands = max_children, max_commands
        self.on_detached = on_detached
        self.on_auth_required = on_auth_required
        self.channels, self.pending_commands = {}, 0
        self.closed, self.failed = False, False
        self.initializers, self.listeners = set(), []

    async def start(self):
        self._listen(self.root)
        async with asyncio.timeout(self.timeout_s):
            await self.root.send("Target.setAutoAttach", _AUTO_ATTACH)
            while self.initializers:
                await asyncio.gather(*list(self.initializers))
        if self.failed:
            raise RuntimeError("child document policy unavailable")

    def _listen(self, parent):
        if parent is not self.root:
            return  # Child events arrive as nested Target messages below.
        for event, method in (("Target.attachedToTarget", self._attached),
                              ("Target.detachedFromTarget", self._detached),
                              ("Target.receivedMessageFromTarget", self._message)):
            handler = lambda payload, method=method: method(parent, payload)
            parent.on(event, handler)
            self.listeners.append((event, handler))

    def fail(self, reason):
        if not self.closed and not self.failed:
            self.failed = True
            self.on_failure(reason)

    def _attached(self, parent, event):
        if self.closed or self.failed or self.is_closed():
            return  # The revoked page is closing; never resume these targets.
        if len(self.channels) >= self.max_children:
            self.fail("child_target_capacity")
            return
        if event["targetInfo"]["type"] != "iframe" or not event.get("waitingForDebugger"):
            self.fail("child_target_not_paused")
            return
        key = (id(parent), event["sessionId"])
        if key in self.channels:
            self.fail("child_target_duplicate")
            return
        child = _ChildChannel(self, parent, event["sessionId"])
        self.channels[key] = child
        task = self.spawn(self._initialize(child))
        if task is not None:
            self.initializers.add(task)
            task.add_done_callback(self.initializers.discard)

    async def _initialize(self, child):
        try:
            async with asyncio.timeout(self.timeout_s):
                await child.send("Fetch.enable", FETCH_OPTIONS)
                await child.send("Target.setAutoAttach", _AUTO_ATTACH)
                if not self.closed and not child.closed and not self.is_closed():
                    await child.send("Runtime.runIfWaitingForDebugger")
                    child.initialized = True
        except asyncio.CancelledError:
            raise
        except Exception:
            if not child.closed and not self.closed and not self.is_closed():
                self.fail("child_target_initialization_failed")

    def _message(self, parent, event):
        child = self.channels.get((id(parent), event["sessionId"]))
        if child is None or child.closed or self.closed:
            return
        try:
            raw = event["message"]
            if len(raw) > 256 * 1024:
                raise ValueError("oversized child message")
            message = json.loads(raw)
            if "id" in message:
                future = child.pending.get(message["id"])
                if future is not None and not future.done():
                    if "error" in message:
                        future.set_exception(RuntimeError("child CDP command failed"))
                    else:
                        future.set_result(message.get("result", {}))
            else:
                method, params = message.get("method"), message.get("params", {})
                if method == "Fetch.requestPaused":
                    self.on_paused(params, child)
                elif method == "Fetch.authRequired":
                    if self.on_auth_required is None:
                        self.fail("child_target_auth_unhandled")
                    else:
                        self.on_auth_required(params, child)
                elif method == "Target.attachedToTarget":
                    self._attached(child, params)
                elif method == "Target.detachedFromTarget":
                    self._detached(child, params)
                elif method == "Target.receivedMessageFromTarget":
                    self._message(child, params)
        except Exception:
            self.fail("child_target_protocol_error")

    def _detached(self, parent, event):
        child = self.channels.pop((id(parent), event["sessionId"]), None)
        if child is None:
            return
        child.close()
        if self.on_detached is not None:
            self.on_detached(child)
        for key, descendant in list(self.channels.items()):
            if descendant.parent is child:
                self._detached(child, {"sessionId": descendant.session_id})

    def close(self):
        # The owning guard revokes and closes the page BEFORE calling this;
        # detaching an active debugger could otherwise release its throttle.
        self.closed = True
        for event, handler in self.listeners:
            self.root.remove_listener(event, handler)
        self.listeners.clear()
        for child in self.channels.values():
            child.close()
        self.channels.clear()
