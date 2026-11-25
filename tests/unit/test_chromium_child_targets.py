"""Child-target interception must be installed before native scripts resume."""
import asyncio
import json
from types import SimpleNamespace

import pytest


class Root:
    def __init__(self):
        self.listeners, self.commands = {}, []
        self.reply = True

    def on(self, name, handler):
        self.listeners[name] = handler

    def remove_listener(self, name, handler):
        self.listeners.pop(name, None)

    async def send(self, method, params):
        self.commands.append((method, params))
        if method == "Target.sendMessageToTarget" and self.reply:
            command = json.loads(params["message"])
            def replies(command):
                yield {"id": command["id"], "result": {}}
                if command["method"] == "Target.sendMessageToTarget":
                    nested = command["params"]
                    for response in replies(json.loads(nested["message"])):
                        yield {"method": "Target.receivedMessageFromTarget", "params": {
                            "sessionId": nested["sessionId"], "message": json.dumps(response),
                        }}
            for response in replies(command):
                self.listeners["Target.receivedMessageFromTarget"]({
                    "sessionId": params["sessionId"], "message": json.dumps(response),
                })
        return {}


def attached():
    return {"sessionId": "child", "waitingForDebugger": True,
            "targetInfo": {"type": "iframe", "targetId": "iframe-target", "url": "http://localhost/frame"}}


async def test_child_fetch_and_recursive_attach_precede_script_resume():
    from app.fetching.chromium_targets import ChildTargets
    root, tasks, failures = Root(), set(), []
    def spawn(coro):
        task = asyncio.create_task(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return task
    manager = ChildTargets(root, on_paused=lambda *a: None, on_failure=failures.append,
        spawn=spawn, is_closed=lambda: False, timeout_s=0.1)
    await manager.start()
    root.listeners["Target.attachedToTarget"](attached())
    await asyncio.gather(*list(tasks))
    methods = [json.loads(params["message"])["method"] for method, params in root.commands if method == "Target.sendMessageToTarget"]
    assert methods == ["Network.enable", "Fetch.enable", "Target.setAutoAttach", "Runtime.runIfWaitingForDebugger"]
    assert failures == []
    manager.close()


async def test_child_setup_timeout_never_resumes_scripts_and_releases_futures():
    from app.fetching.chromium_targets import ChildTargets
    root, tasks, failures = Root(), set(), []
    root.reply = False
    def spawn(coro):
        task = asyncio.create_task(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return task
    manager = ChildTargets(root, on_paused=lambda *a: None, on_failure=failures.append,
        spawn=spawn, is_closed=lambda: False, timeout_s=0.01)
    await manager.start()
    root.listeners["Target.attachedToTarget"](attached())
    await asyncio.gather(*list(tasks))
    methods = [json.loads(params["message"])["method"] for method, params in root.commands if method == "Target.sendMessageToTarget"]
    assert methods == ["Network.enable"]
    assert failures == ["child_target_initialization_failed"]
    assert manager.pending_commands == 0
    manager.close()
    assert not manager.channels


async def test_child_capacity_fails_closed_without_spawning_extra_tasks():
    from app.fetching.chromium_targets import ChildTargets
    failures, spawned = [], []
    def spawn(coro):
        spawned.append(coro)
        raise AssertionError("capacity overflow must not spawn")
    root = Root()
    manager = ChildTargets(root, on_paused=lambda *a: None, on_failure=failures.append,
        spawn=spawn, is_closed=lambda: False, max_children=0)
    await manager.start()
    root.listeners["Target.attachedToTarget"](attached())
    assert failures == ["child_target_capacity"]
    assert spawned == []
    manager.close()


async def test_recursive_child_target_gets_its_own_interception_before_resume():
    from app.fetching.chromium_targets import ChildTargets
    root, tasks, failures = Root(), set(), []
    def spawn(coro):
        task = asyncio.create_task(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return task
    manager = ChildTargets(root, on_paused=lambda *a: None, on_failure=failures.append,
        spawn=spawn, is_closed=lambda: False, timeout_s=0.1)
    await manager.start()
    root.listeners["Target.attachedToTarget"](attached())
    await asyncio.gather(*list(tasks))
    nested = attached()
    nested["sessionId"] = "grandchild"
    root.listeners["Target.receivedMessageFromTarget"]({"sessionId": "child", "message": json.dumps({
        "method": "Target.attachedToTarget", "params": nested,
    })})
    await asyncio.gather(*list(tasks))
    leaves = []
    for method, params in root.commands:
        if method != "Target.sendMessageToTarget":
            continue
        message = json.loads(params["message"])
        while message["method"] == "Target.sendMessageToTarget":
            message = json.loads(message["params"]["message"])
        leaves.append(message["method"])
    assert leaves == ["Network.enable", "Fetch.enable", "Target.setAutoAttach", "Runtime.runIfWaitingForDebugger"] * 2
    assert failures == []
    manager.close()


async def test_native_child_redirect_rejection_uses_child_channel(monkeypatch):
    from app.fetching.chromium_targets import ChildTargets
    from app.fetching.chromium_destinations import ChromiumDocumentGuard
    from app.fetching.oneoff import PrivateAddress
    root = Root()
    guard = ChromiumDocumentGuard(object(), "https://catalogue.test/list", destination_policy=lambda url: False)
    guard.session, guard.main_frame_id = root, "main"
    async def private(url):
        return url.endswith("/private-target")
    monkeypatch.setattr("app.fetching.chromium_destinations.url_is_private", private)
    manager = ChildTargets(root, on_paused=guard._on_paused, on_failure=guard._child_failure,
        spawn=guard._spawn, is_closed=lambda: guard.closed, timeout_s=0.1)
    await manager.start()
    root.listeners["Target.attachedToTarget"](attached())
    await asyncio.gather(*list(guard.tasks))
    root.listeners["Target.receivedMessageFromTarget"]({"sessionId": "child", "message": json.dumps({
        "method": "Fetch.requestPaused", "params": {"requestId": "response", "frameId": "iframe-target",
            "request": {"url": "http://localhost/redirect"}, "responseStatusCode": 302,
            "responseHeaders": [{"name": "Location", "value": "http://127.0.0.1/private-target"}]},
    })})
    await asyncio.gather(*list(guard.tasks))
    method, params = root.commands[-1]
    assert method == "Target.sendMessageToTarget" and params["sessionId"] == "child"
    assert json.loads(params["message"])["method"] == "Fetch.failRequest"
    assert isinstance(guard.errors[0], PrivateAddress)
    manager.close()


async def test_initialized_child_command_timeout_revokes_owning_guard():
    from app.fetching.chromium_targets import ChildTargets
    root, tasks, failures = Root(), set(), []
    def spawn(coro):
        task = asyncio.create_task(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return task
    manager = ChildTargets(root, on_paused=lambda *a: None, on_failure=failures.append,
        spawn=spawn, is_closed=lambda: False, timeout_s=0.01)
    await manager.start()
    root.listeners["Target.attachedToTarget"](attached())
    await asyncio.gather(*list(tasks))
    child = next(iter(manager.channels.values()))
    root.reply = False
    with pytest.raises(TimeoutError):
        await child.send("Fetch.failRequest", {"requestId": "denied", "errorReason": "BlockedByClient"})
    assert failures == ["child_target_command_failed"]
    assert manager.pending_commands == 0
    manager.close()
