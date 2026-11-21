"""Synthetic upstream authentication failures must retain resumable job work."""

import asyncio
import copy
from types import SimpleNamespace

import httpx
import pytest
from crawlee import Request
from crawlee.errors import ContextPipelineInitializationError
from playwright.async_api import Error

from app.core.config import merge_layers
from app.core.db import loads
from app.core.schemas import validator
from app.jobs.runner import Deferred, JobRunner
from app.service import Service
from tests.conftest import platform, profile
from tests.unit.test_logic import settings


@pytest.mark.parametrize("error", [
    httpx.ProxyError("407 Proxy Authentication Required"),
    Error("NS_ERROR_PROXY_AUTHENTICATION_FAILED via http://secret:password@proxy.test"),
    Error("net::ERR_PROXY_AUTH_REQUESTED"),
    RuntimeError("CONNECT tunnel failed, response 407"),
])
def test_auth_classifier_recognizes_explicit_upstream_rejection(error):
    from app.fetching.proxy_auth import is_proxy_auth_failure

    assert is_proxy_auth_failure(error, proxy_used=True)
    assert is_proxy_auth_failure(ContextPipelineInitializationError(error, None), proxy_used=True)
    assert not is_proxy_auth_failure(error, proxy_used=False)


@pytest.mark.parametrize("error,status", [
    (httpx.ProxyError("proxy connection timed out"), None),
    (Error("net::ERR_TUNNEL_CONNECTION_FAILED"), None),
    (Error("net::ERR_INVALID_AUTH_CREDENTIALS"), None),
    (RuntimeError("Cloudflare HTTP 403"), 403),
    (RuntimeError("origin unavailable"), 503),
    (RuntimeError("https://target.test/407/items"), None),
])
def test_auth_classifier_does_not_confuse_target_or_transient_failures(error, status):
    from app.fetching.proxy_auth import is_proxy_auth_failure

    assert not is_proxy_auth_failure(error, status=status, proxy_used=True)


async def make_run(tmp_path, monkeypatch, *, connection_profile=None, config_overrides=None):
    svc = Service(settings(data_dir=str(tmp_path), secrets_key="fixture-only-key-0123456789abcdef"))
    monkeypatch.setattr("app.settings._settings", svc.settings)
    monkeypatch.setattr("app.fetching.browsers.resolve_browser", lambda _: None)
    monkeypatch.setattr("app.fetching.browsers.browser_presence", lambda: {"camoufox": {"present": True}, "chromium": {"present": True}})
    config, _ = merge_layers({
        "discovery_mode": "url_list", "urls": [{"url": "https://target.test/one"}, {"url": "https://target.test/two"}],
        "fetch_mode": "http", "respect_robots": False, "max_retries": 0,
        "delay_min_seconds": 0, "delay_max_seconds": 0,
        **(config_overrides or {}),
    }, {}, {})
    _, summary = await svc.jobs.create({"config": config, "profile": connection_profile or profile(), "platform": platform("proxy-auth-fixture")}, "proxy-auth-fixture")
    run_id = summary["run_id"]
    svc.jobs._set_state(run_id, "running")
    runner = JobRunner(svc, run_id)
    return svc, runner


async def test_pause_keeps_secrets_records_and_source_checkpoint(tmp_path, monkeypatch):
    svc, runner = await make_run(tmp_path, monkeypatch)
    runner.doc["secrets"]["proxy_and_solver"] = "kept"
    runner.counters()["records_extracted"] = 1
    runner.rt.update({"sitemap_traversal": {"queue": ["https://target.test/two.xml"]}, "navigation_actions": 7, "running_seconds": 12.0})
    runner.prot()["clicks_used"] = 2
    runner.prot()["solves_used"] = 3
    before = copy.deepcopy(runner.rt)
    stopped = []
    runner.crawler = SimpleNamespace(stop=lambda *args: stopped.append(True))
    try:
        await runner._pause_for_proxy_auth()
        await runner._pause_for_proxy_auth()
        row = svc.db.one("SELECT * FROM runs WHERE run_id=?", (runner.run_id,))
        doc, rt = loads(row["doc"]), loads(row["runtime"])
        assert row["state"] == "waiting_for_credentials"
        assert doc["secrets"]["proxy_and_solver"] == "kept"
        assert doc["counters"]["records_extracted"] == 1
        assert doc["protection"]["clicks_used"] == 2
        assert doc["protection"]["solves_used"] == 3
        assert rt["sitemap_traversal"] == before["sitemap_traversal"]
        assert rt["running_seconds"] == 12
        assert rt["navigation_actions"] == 7
        assert not rt.get("feeder_done")
        assert not rt.get("stop_reason")
        events = svc.db.all("SELECT event_id FROM events WHERE run_id=?", (runner.run_id,))
        assert len(events) == 1
        event = svc.events.payload(events[0]["event_id"])
        assert event["problem_code"] == "proxy_auth_failed"
        assert "Resume" in event["message"]
        assert not list(validator("event.schema.json").iter_errors(event))
        assert stopped == [True]
    finally:
        svc.db.close()


async def test_auxiliary_auth_failure_propagates_without_becoming_empty_response(tmp_path, monkeypatch):
    from app.fetching.oneoff import Fetched
    from app.fetching.proxy_auth import ProxyAuthenticationRequired
    from app.fetching.proxies import TierPlan

    svc, runner = await make_run(tmp_path, monkeypatch)
    runner.tiers = TierPlan([{"tier": 1, "provider": "generic", "protocol": "http", "host": "proxy.test", "port": 8080, "auth_mode": "ip_allowlist"}])

    async def public(_):
        return False

    async def reject(*args, **kwargs):
        return Fetched(args[0], args[0], None, error="proxy_auth_failed")

    monkeypatch.setattr("app.jobs.runner.url_is_private", public)
    monkeypatch.setattr("app.fetching.oneoff.http_get", reject)
    try:
        with pytest.raises(ProxyAuthenticationRequired):
            await runner.aux_fetch("https://target.test/two.xml")
        assert runner.state == "waiting_for_credentials"
        assert not runner.rt.get("feeder_done")
    finally:
        svc.db.close()


async def test_http_connect_407_has_sanitized_auth_classification():
    from app.fetching.oneoff import http_get

    requests = []

    async def refusing(reader, writer):
        requests.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b'HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm="fixture"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(refusing, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        result = await http_get("https://target.test/document", proxy=f"http://secret-user:secret-password@127.0.0.1:{port}", check_private=False)
        assert result.error == "proxy_auth_failed"
        assert len(requests) == 1
        assert requests[0].startswith(b"CONNECT target.test:443")
        assert "secret-" not in repr(result)
    finally:
        server.close()
        await server.wait_closed()


async def test_real_crawler_queue_survives_pause_restart_and_resume(tmp_path, monkeypatch):
    """A local proxy supplies one target, rejects the next, then approves Resume."""
    from app.core.config import profile_summary, secret_part
    from app.fetching.proxies import TierPlan

    svc, runner = await make_run(tmp_path, monkeypatch)
    svc.settings.supervisor_interval_seconds = 0.01
    approved = False
    requests = []

    async def proxy(reader, writer):
        try:
            data = await reader.readuntil(b"\r\n\r\n")
            path = data.split(b" ")[1].decode()
            requests.append(path)
            if path.endswith("/two") and not approved:
                response = b'HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm="fixture"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n'
            else:
                body = ('<main class="sheet"><h1>Item ' + path.rsplit('/', 1)[-1] + '</h1><p>' + 'A complete product description. ' * 40 + '</p></main>').encode()
                response = b'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: ' + str(len(body)).encode() + b'\r\nConnection: close\r\n\r\n' + body
            writer.write(response)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(proxy, "127.0.0.1", 0)
    tier = {"tier": 1, "provider": "generic", "protocol": "http", "host": "127.0.0.1", "port": server.sockets[0].getsockname()[1], "auth_mode": "ip_allowlist"}
    # HTTP deliberately avoids TLS to let this refusing proxy stay self-contained.
    runner.config["urls"] = [{"url": "http://target.test/one"}, {"url": "http://target.test/two"}]
    runner.config.update(use_proxy_tiers=[1], fields=["title"], max_concurrency=1, max_requests_per_minute=60000, respect_robots=False)
    runner.doc["profile"] = profile_summary(profile(proxy_tiers=[tier]))
    assert runner.rt["needs_secrets"] is False
    svc.secrets.put(runner.run_id, "proxy_solver", secret_part(profile(proxy_tiers=[tier])))
    runner.doc["secrets"]["proxy_and_solver"] = "kept"
    svc.mappings.activate("target.test", "sheet", 0, {
        "schema_version": 1, "template_id": "sheet", "match": {"selector": ".sheet"},
        "fields": [{"name": "title", "type": "text", "required": True, "sources": [{"kind": "css", "selector": "h1", "extract": "text"}]}],
    }, None)
    runner._persist()
    try:
        runner.task = asyncio.create_task(runner._main())
        await asyncio.wait_for(runner.task, 15)
        assert runner.state == "waiting_for_credentials"
        assert requests == ["http://target.test/one", "http://target.test/two"]
        assert svc.db.scalar("SELECT COUNT(*) FROM records WHERE run_id=?", (runner.run_id,)) == 1
        assert svc.db.scalar("SELECT COUNT(*) FROM unextracted WHERE run_id=?", (runner.run_id,)) == 0
        pending = await runner.rq.get_request(runner._ukey("http://target.test/two", None))
        assert pending.handled_at is None
        assert pending.retry_count == 0
        assert not runner.rt.get("host_trouble")
        assert not runner.tiers.host_level
        assert len(svc.events.for_run(runner.run_id)) == 1
        run_id = runner.run_id
        paused_seconds = runner.rt["running_seconds"]
        svc.db.close()

        # Reopening SQLite and Crawlee storage simulates a restart; no stored/demo job is used.
        svc = Service(settings(data_dir=str(tmp_path), secrets_key="fixture-only-key-0123456789abcdef", supervisor_interval_seconds=0.01))
        monkeypatch.setattr("app.settings._settings", svc.settings)
        await svc.jobs._recover()
        assert svc.jobs.runners == {}
        approved = True
        await svc.jobs.resume(run_id, profile(proxy_tiers=[tier]))
        resumed = svc.jobs.runners[run_id]
        await asyncio.wait_for(resumed.task, 15)
        assert svc.jobs.summary(run_id)["state"] == "completed"
        assert requests == ["http://target.test/one", "http://target.test/two", "http://target.test/two"]
        assert svc.db.scalar("SELECT COUNT(*) FROM records WHERE run_id=?", (run_id,)) == 2
        assert resumed.rt["running_seconds"] >= paused_seconds
        assert resumed.pages_started == 3
        assert not resumed.tiers.host_level
    finally:
        if runner.task and not runner.task.done():
            await runner.shutdown(budget=2)
        server.close()
        await server.wait_closed()
        svc.db.close()


@pytest.mark.parametrize("mode", ["auto", "browser"])
async def test_browser_pipeline_auth_error_defers_before_fallback_or_retry(tmp_path, monkeypatch, caplog, mode):
    """Real adaptive dispatch, synthetic Firefox error; no browser binary is launched."""
    from app.fetching.proxies import TierPlan
    from app.jobs.proxy_auth import guard_proxy_auth

    svc, runner = await make_run(tmp_path, monkeypatch)
    runner.browser = "chromium"
    runner.config["fetch_mode"] = mode
    runner.tiers = TierPlan([{"tier": 1, "provider": "generic", "protocol": "http", "host": "proxy.test", "port": 8080, "auth_mode": "ip_allowlist"}])
    await runner._open_storages()
    calls = []
    try:
        crawler = runner._build_crawler()
        runner.crawler = crawler
        # Replace transport at its pipeline boundary, retaining Crawlee's dispatch,
        # deferral, queue and stop behavior. Camoufox is absent on the host.
        def pipeline(rendering_type, context_linked_to_result):
            async def reject():
                calls.append(rendering_type)
                raise Error("NS_ERROR_PROXY_AUTHENTICATION_FAILED via http://secret-user:secret-password@proxy.test")
            return reject()

        crawler._pipeline_call_factory = pipeline
        guard_proxy_auth(crawler, runner, adaptive=True)
        await runner.rq.add_requests([Request.from_url("https://target.test/two")])
        await asyncio.wait_for(crawler.run([]), 10)
        assert runner.state == "waiting_for_credentials"
        assert "secret-user" not in caplog.text
        assert "secret-password" not in caplog.text
        assert calls == (["static"] if mode == "auto" else ["client only"])
        pending = await runner.rq.get_request("https://target.test/two")
        assert pending.handled_at is None
        assert pending.retry_count == 0
        assert (pending.session_rotation_count or 0) == 0
        assert len(svc.events.for_run(runner.run_id)) == 1
        assert "secret-" not in str(svc.events.payload(svc.events.for_run(runner.run_id)[0]["event_id"]))
    finally:
        svc.db.close()


async def test_resume_waits_for_old_queue_owner_and_starts_only_once(tmp_path, monkeypatch):
    svc, runner = await make_run(tmp_path, monkeypatch)
    await runner._pause_for_proxy_auth()
    released = asyncio.Event()
    old_done = []

    async def teardown():
        await released.wait()
        old_done.append(True)

    runner.task = asyncio.create_task(teardown())
    svc.jobs.runners[runner.run_id] = runner
    starts = []

    def start(run_id):
        assert old_done
        starts.append(run_id)

    monkeypatch.setattr(svc.jobs, "_start_runner", start)
    try:
        first = asyncio.create_task(svc.jobs.resume(runner.run_id, {}))
        second = asyncio.create_task(svc.jobs.resume(runner.run_id, {}))
        await asyncio.sleep(0)
        assert starts == []
        released.set()
        await asyncio.gather(first, second)
        assert starts == [runner.run_id]
    finally:
        released.set()
        await runner.task
        svc.db.close()


async def test_pause_does_not_delete_link_backlog(tmp_path, monkeypatch):
    svc, runner = await make_run(tmp_path, monkeypatch)
    svc.db.execute("INSERT INTO link_backlog(run_id,url,source,unique_key,forefront) VALUES (?,?,?,?,?)", (runner.run_id, "https://target.test/next", "link", "next", 0))
    try:
        await runner._pause_for_proxy_auth()
        await runner._drain_backlog()
        assert svc.db.scalar("SELECT COUNT(*) FROM link_backlog WHERE run_id=?", (runner.run_id,)) == 1
    finally:
        svc.db.close()


async def test_undecryptable_secret_pause_still_reports_missing(tmp_path, monkeypatch):
    svc, runner = await make_run(tmp_path, monkeypatch)
    try:
        await svc.jobs.to_waiting_for_credentials(runner.run_id)
        assert svc.jobs.summary(runner.run_id)["secrets"]["proxy_and_solver"] == "missing"
        event = svc.events.for_run(runner.run_id)[0]
        assert svc.events.payload(event["event_id"])["problem_code"] == "waiting_for_credentials"
    finally:
        svc.db.close()


async def test_resumed_auxiliary_fetch_reuses_pending_queue_request(tmp_path, monkeypatch):
    from app.fetching.oneoff import Fetched

    svc, runner = await make_run(tmp_path, monkeypatch)
    await runner._open_storages()
    url = "https://target.test/pending-sitemap.xml"
    pending_key = "aux\x1f" + url + "\x1fexisting"
    await runner.rq.add_requests([Request.from_url(url, unique_key=pending_key, user_data={"aux": True, "src": "aux"})])
    runner.rt["aux_pending"] = {url: pending_key}
    runner._persist()
    resumed = JobRunner(svc, runner.run_id)

    async def public(_):
        return False

    monkeypatch.setattr("app.jobs.runner.url_is_private", public)
    busy = asyncio.Event()
    resumed.crawler_task = asyncio.create_task(busy.wait())
    resumed.crawler = SimpleNamespace(stop=lambda *args: None)
    try:
        await resumed._prepare()
        # The queued response may arrive before its source feeder asks for it.
        resumed._aux_done(SimpleNamespace(unique_key=pending_key), Fetched(url, url, 200, b"https://target.test/document"))
        fetched = await asyncio.wait_for(resumed.aux_fetch(url), 2)
        assert fetched.status == 200
        meta = await resumed.rq.get_metadata()
        assert meta.total_request_count == 1
        assert resumed.rt.get("aux_pending", {}) == {}
    finally:
        busy.set()
        await resumed.crawler_task
        svc.db.close()


async def test_a_later_rejection_after_resume_creates_one_new_actionable_problem(tmp_path, monkeypatch):
    svc, runner = await make_run(tmp_path, monkeypatch)
    try:
        await runner._pause_for_proxy_auth()
        await runner._pause_for_proxy_auth()
        svc.jobs._set_state(runner.run_id, "running")
        await runner._pause_for_proxy_auth()
        await runner._pause_for_proxy_auth()
        assert len(svc.events.for_run(runner.run_id)) == 2
    finally:
        svc.db.close()


async def test_pause_from_supervisor_owned_lock_does_not_deadlock(tmp_path, monkeypatch):
    svc, runner = await make_run(tmp_path, monkeypatch)
    try:
        async with runner.lock:
            await asyncio.wait_for(runner._pause_for_proxy_auth(), 1)
        assert runner.state == "waiting_for_credentials"
    finally:
        svc.db.close()


@pytest.mark.parametrize("transport_error", [False, True])
async def test_browser_auxiliary_raw_fetch_pauses_on_auth_rejection(tmp_path, monkeypatch, transport_error):
    """The byte request can lose proxy access after browser navigation succeeds."""
    from app.fetching.proxies import TierPlan

    svc, runner = await make_run(tmp_path, monkeypatch)
    runner.tiers = TierPlan([{"tier": 1}])
    request = Request.from_url("https://target.test/sitemap.xml", user_data={"aux": True})

    async def settled(*args):
        return True

    async def no_challenge(*args):
        return SimpleNamespace(is_block=False, is_challenge=False)

    async def get(*args, **kwargs):
        if transport_error:
            raise Error("NS_ERROR_PROXY_AUTHENTICATION_FAILED with fixture-password")
        async def body():
            return b""
        return SimpleNamespace(status=407, url=request.url, headers={}, body=body)

    monkeypatch.setattr("app.fetching.challenge.settle", settled)
    monkeypatch.setattr("app.jobs.runner.detect_page", no_challenge)
    ctx = SimpleNamespace(request=request, page=SimpleNamespace(context=SimpleNamespace(request=SimpleNamespace(get=get))))
    try:
        with pytest.raises(Deferred):
            await runner._handle_aux(ctx, "target.test", 200, True)
        assert runner.state == "waiting_for_credentials"
        assert runner.prot()["hard_blocks"] == 0
        assert runner.prot()["challenges_met"] == 0
    finally:
        svc.db.close()


@pytest.mark.parametrize("browser_run,transport_error", [(False, False), (True, False), (False, True), (True, True)])
async def test_variant_json_auth_failure_pauses_the_original_document(tmp_path, monkeypatch, browser_run, transport_error):
    from app.extraction.page import Page
    from app.extraction.variants import resolve
    from app.fetching.proxies import TierPlan
    from app.fetching.proxy_auth import ProxyAuthenticationRequired

    svc, runner = await make_run(tmp_path, monkeypatch)
    runner.tiers = TierPlan([{"tier": 1}])
    runner.config["respect_robots"] = False

    async def public(_):
        return False

    async def get(*args, **kwargs):
        if transport_error:
            raise Error("NS_ERROR_PROXY_AUTHENTICATION_FAILED with fixture-password")
        return SimpleNamespace(status=407, status_code=407)

    monkeypatch.setattr("app.jobs.runner.url_is_private", public)
    ctx = SimpleNamespace(send_request=get, page=SimpleNamespace(context=SimpleNamespace(request=SimpleNamespace(get=get))))
    try:
        page = Page("<main>Product</main>", "https://target.test/products/lamp")
        with pytest.raises(ProxyAuthenticationRequired):
            await resolve(page, {"sources": [{"kind": "shop_json", "platform": "shopify"}]}, runner._variant_fetcher(ctx, browser_run))
        assert runner.state == "waiting_for_credentials"
    finally:
        svc.db.close()


async def test_listing_pause_cancels_actions_and_keeps_original_request_pending(tmp_path, monkeypatch):
    from app.extraction.page import Page
    from app.fetching.navigation import NavigationAction, NavigationResult

    svc, runner = await make_run(tmp_path, monkeypatch)
    runner.config["site_url"] = "https://target.test/list"
    request = Request.from_url(runner.config["site_url"])
    observed = []

    async def collect(page, **kwargs):
        await runner._pause_for_proxy_auth()
        observed.append(kwargs["cancelled"]())
        observed.append(await kwargs["before_action"](NavigationAction("navigation", "More", runner.config["site_url"])))
        return NavigationResult()

    monkeypatch.setattr("app.fetching.navigation.collect_listing_links", collect)
    try:
        with pytest.raises(Deferred):
            await runner._collect_navigation(SimpleNamespace(request=request, page=object()), Page("<main>Listing</main>", runner.config["site_url"]), runner.config["site_url"], True, {"src": "start"}, {})
        assert observed == [True, False]
        assert runner.rt.get("navigation_actions", 0) == 0
    finally:
        svc.db.close()


async def test_stop_during_paused_teardown_starts_finalization_after_queue_release(tmp_path, monkeypatch):
    svc, runner = await make_run(tmp_path, monkeypatch)
    await runner._pause_for_proxy_auth()
    svc.jobs.runners[runner.run_id] = runner
    starts = []
    monkeypatch.setattr(svc.jobs, "_start_runner", lambda run_id: starts.append(run_id))
    try:
        await svc.jobs.stop(runner.run_id)
        assert starts == []
        svc.jobs.runner_done(runner.run_id)
        assert starts == [runner.run_id]
        assert svc.jobs.summary(runner.run_id)["state"] == "stopping"
    finally:
        svc.db.close()


async def test_auxiliary_browser_does_not_start_raw_request_after_concurrent_pause(tmp_path, monkeypatch):
    svc, runner = await make_run(tmp_path, monkeypatch)
    request = Request.from_url("https://target.test/sitemap.xml", user_data={"aux": True})
    called = []

    async def settled(*args):
        await runner._pause_for_proxy_auth()
        return True

    async def no_challenge(*args):
        return SimpleNamespace(is_block=False, is_challenge=False)

    async def get(*args, **kwargs):
        called.append(True)
        raise AssertionError("raw fetch started after proxy authentication pause")

    monkeypatch.setattr("app.fetching.challenge.settle", settled)
    monkeypatch.setattr("app.jobs.runner.detect_page", no_challenge)
    ctx = SimpleNamespace(request=request, page=SimpleNamespace(context=SimpleNamespace(request=SimpleNamespace(get=get))))
    try:
        with pytest.raises(Deferred):
            await runner._handle_aux(ctx, "target.test", 200, True)
        assert called == []
    finally:
        svc.db.close()
