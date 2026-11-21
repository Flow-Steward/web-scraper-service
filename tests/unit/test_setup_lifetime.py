"""Setup discovery metadata is bounded and belongs to the caller."""

import asyncio
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from app import setup_ops as setup
from app.api import routes
from app.core.errors import ApiError
from app.core.schemas import schema_errors
from app.fetching.oneoff import Fetched

SITE = "https://supplier.test/"
INDEX = SITE + "sitemap.xml"


class SyntheticSetup:
    browser = None
    failures = []
    traces = []

    def __init__(self, documents):
        self.documents = documents
        self.calls = []
        self.slow_started = asyncio.Event()
        self.slow_closed = asyncio.Event()

    def failure_for(self, url):
        return None

    async def http(self, url):
        return Fetched(url, url, 200, ("<main>" + "visible words " * 100 + "</main>").encode())

    async def fetch(self, url):
        self.calls.append(url)
        body = self.documents.get(url, b"")
        if body is None:
            self.slow_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.slow_closed.set()
        return Fetched(url, url, 200 if body else 404, body)


def urlset(count):
    return ("<urlset>" + "".join(f"<url><loc>{SITE}item/{i}</loc></url>" for i in range(count)) + "</urlset>").encode()


def index(urls):
    return ("<sitemapindex>" + "".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in urls) + "</sitemapindex>").encode()


@pytest.fixture(autouse=True)
def synthetic_only(monkeypatch):
    async def public(url):
        return False

    async def no_robots(*args):
        return None

    monkeypatch.setattr(setup, "_robots", no_robots)
    monkeypatch.setattr(setup, "url_is_private", public)
    monkeypatch.setattr("app.jobs.discovery.url_is_private", public)


def service():
    return SimpleNamespace(mappings=SimpleNamespace(active_mappings=lambda site: []), settings=SimpleNamespace(setup_timeout_seconds=10))


@pytest.mark.parametrize("mode", ["crawl", "url_list"])
async def test_explicit_discovery_skips_sitemap_inventory(monkeypatch, mode):
    sf = SyntheticSetup({INDEX: urlset(10)})
    result = await setup._analyze(service(), {"discovery_mode": mode}, SITE, sf)
    assert sf.calls == []
    assert result["recommended"]["discovery_mode"] == mode
    assert result["sitemap"]["inspection"] == "skipped"
    assert result["sitemap"]["found"] is None
    assert result["sitemap"]["url_count"] is None
    assert result["sitemap"]["matching_url_count"] is None
    assert not schema_errors("analysis.schema.json", result)


async def test_auto_analysis_limits_observed_urls(monkeypatch):
    monkeypatch.setattr(setup, "SITEMAP_METADATA_URL_CAP", 3, raising=False)
    sf = SyntheticSetup({INDEX: urlset(10)})
    result = await setup._analyze(service(), {"discovery_mode": "auto"}, SITE, sf)
    assert result["sitemap"]["url_count"] == 3
    assert result["sitemap"]["url_count_capped"] is True
    assert result["sitemap"]["inspection"] == "limited"
    assert result["sitemap"]["target_share"] is None
    assert not schema_errors("analysis.schema.json", result)


async def test_auto_analysis_limits_nested_files(monkeypatch):
    monkeypatch.setattr(setup, "SITEMAP_METADATA_FILE_CAP", 2, raising=False)
    children = [SITE + f"child-{i}.xml" for i in range(8)]
    sf = SyntheticSetup({INDEX: index(children), **{u: urlset(1) for u in children}})
    result = await setup._analyze(service(), {"discovery_mode": "auto", "sitemap_url": INDEX}, SITE, sf)
    assert len(sf.calls) == 2
    assert result["sitemap"]["inspection"] == "limited"
    assert result["sitemap"]["url_count_capped"] is True


async def test_slow_nested_metadata_preserves_partial_count_and_cancels_fetch(monkeypatch):
    monkeypatch.setattr(setup, "SITEMAP_METADATA_SECONDS", 0.02, raising=False)
    child, slow = SITE + "child.xml", SITE + "slow.xml"
    sf = SyntheticSetup({INDEX: index([child, slow]), child: urlset(1), slow: None})
    result = await asyncio.wait_for(setup._analyze(service(), {"discovery_mode": "auto", "sitemap_url": INDEX}, SITE, sf), 0.5)
    assert sf.slow_closed.is_set()
    assert result["sitemap"]["url_count"] == 1
    assert result["sitemap"]["inspection"] == "limited"
    assert result["sitemap"]["url_count_capped"] is True
    assert not schema_errors("analysis.schema.json", result)


async def test_genuine_sitemap_control_keeps_metadata_and_recommendation():
    sf = SyntheticSetup({INDEX: urlset(2)})
    result = await setup._analyze(service(), {"discovery_mode": "auto"}, SITE, sf)
    assert result["sitemap"]["found"] is True
    assert result["sitemap"]["url_count"] == 2
    assert result["sitemap"]["inspection"] == "complete"
    assert result["sitemap"]["url_count_capped"] is False
    assert result["recommended"]["discovery_mode"] == "sitemap"
    assert not schema_errors("analysis.schema.json", result)


@pytest.mark.parametrize("endpoint", ["analyze", "samples"])
@pytest.mark.parametrize("stop", ["disconnect", "deadline", "cancellation"])
async def test_setup_route_stops_and_awaits_request_work(monkeypatch, endpoint, stop):
    started, closed, disconnect = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def read(*args):
        return {}

    async def run(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)  # cleanup must finish before the route returns
            closed.set()

    async def receive():
        await disconnect.wait()
        return {"type": "http.disconnect"}

    monkeypatch.setattr(routes, "read_json", read)
    svc = service()
    svc.settings.setup_timeout_seconds = 0.02 if stop == "deadline" else 10
    monkeypatch.setattr(routes, "get_service", lambda: svc)
    monkeypatch.setattr(setup, endpoint, run)
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []}, receive=receive)
    task = asyncio.create_task(getattr(routes, endpoint)(request))
    await started.wait()
    if stop == "disconnect":
        disconnect.set()
    elif stop == "cancellation":
        task.cancel()
    if stop == "cancellation":
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(ApiError) as error:
            await asyncio.wait_for(task, 0.5)
        assert error.value.status == 503
        assert error.value.details["reason"] == ("caller_disconnected" if stop == "disconnect" else "setup_deadline_exceeded")
    assert closed.is_set()


@pytest.mark.parametrize("resource", ["http_stream", "browser_start"])
@pytest.mark.parametrize("stop", ["disconnect", "deadline"])
async def test_setup_caller_stop_closes_real_setup_resources(tmp_path, monkeypatch, resource, stop):
    import httpx

    from app.core.config import merge_layers
    from app.fetching.oneoff import BrowserSession
    from app.service import Service
    from tests.unit.test_logic import settings

    svc = Service(settings(data_dir=str(tmp_path), secrets_key="test-key-012345678901234567"))
    svc.settings.setup_timeout_seconds = 0.05 if stop == "deadline" else 10
    monkeypatch.setattr("app.settings._settings", svc.settings)
    monkeypatch.setattr(routes, "get_service", lambda: svc)
    monkeypatch.setattr("app.fetching.browsers.resolve_browser", lambda _: "chromium" if resource == "browser_start" else None)
    started, disconnect = asyncio.Event(), asyncio.Event()
    closed = []

    async def public(url):
        return False

    monkeypatch.setattr("app.fetching.oneoff.url_is_private", public)

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            await asyncio.Event().wait()
            yield b"unreachable"

        async def aclose(self):
            closed.append("stream")

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(200, stream=Stream())

        async def aclose(self):
            closed.append("transport")

    monkeypatch.setattr("app.fetching.oneoff.guarded_transport", lambda **kwargs: Transport())

    if resource == "browser_start":
        async def http(url, **kwargs):
            return Fetched(url, url, 200, b"<main>Needs rendering</main>")

        async def close_context():
            await asyncio.sleep(0)
            closed.append("context")

        async def stop_playwright():
            closed.append("playwright")

        async def start(self):
            self.context = SimpleNamespace(close=close_context)
            self._pw = SimpleNamespace(stop=stop_playwright)
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(setup, "http_get", http)
        monkeypatch.setattr(BrowserSession, "start", start)

    config, _ = merge_layers({"site_url": SITE, "discovery_mode": "crawl", "delay_min_seconds": 0, "delay_max_seconds": 0}, {}, {})

    async def read(*args):
        return {"config": config, "profile": {"proxy_tiers": []}}

    async def receive():
        await disconnect.wait()
        return {"type": "http.disconnect"}

    monkeypatch.setattr(routes, "read_json", read)
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []}, receive=receive)
    task = asyncio.create_task(routes.analyze(request))
    try:
        await asyncio.wait_for(started.wait(), 0.5)
        if stop == "disconnect":
            disconnect.set()
        with pytest.raises(ApiError) as error:
            await asyncio.wait_for(task, 0.5)
        assert error.value.status == 503
        assert closed == (["stream", "transport"] if resource == "http_stream" else ["context", "playwright"])
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        svc.db.close()


async def test_metadata_timeout_before_any_urls_is_unknown(monkeypatch):
    monkeypatch.setattr(setup, "SITEMAP_METADATA_SECONDS", 0.01)
    sf = SyntheticSetup({INDEX: None})
    result = await setup._analyze(service(), {"discovery_mode": "auto", "sitemap_url": INDEX}, SITE, sf)
    assert sf.slow_closed.is_set()
    assert result["sitemap"]["inspection"] == "limited"
    assert result["sitemap"]["found"] is None
    assert result["sitemap"]["url_count"] is None
    assert result["sitemap"]["matching_url_count"] is None
    assert "no sitemap found" not in result["recommended"]["reasons"]
    assert not schema_errors("analysis.schema.json", result)


async def test_completed_setup_route_cancels_disconnect_watcher(monkeypatch):
    watcher_closed = asyncio.Event()

    async def read(*args):
        return {}

    async def run(*args):
        await asyncio.sleep(0)
        return {"ok": True}

    async def receive():
        try:
            await asyncio.Event().wait()
        finally:
            watcher_closed.set()

    monkeypatch.setattr(routes, "read_json", read)
    monkeypatch.setattr(routes, "get_service", service)
    monkeypatch.setattr(setup, "analyze", run)
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []}, receive=receive)
    response = await routes.analyze(request)
    assert response.body == b'{"ok":true}'
    assert watcher_closed.is_set()


@pytest.mark.parametrize("endpoint", ["analyze", "samples"])
async def test_asgi_disconnect_after_validated_body_stops_setup(tmp_path, monkeypatch, endpoint):
    import json

    from app.core.config import merge_layers
    from app.main import create_app
    from app.service import Service
    from tests.conftest import profile
    from tests.unit.test_logic import settings

    svc = Service(settings(data_dir=str(tmp_path), secrets_key="test-key-012345678901234567"))
    monkeypatch.setattr("app.settings._settings", svc.settings)
    monkeypatch.setattr("app.service._service", svc)
    started, closed = asyncio.Event(), asyncio.Event()
    messages = asyncio.Queue()
    config, _ = merge_layers({"site_url": SITE, "discovery_mode": "crawl"}, {}, {})
    body = json.dumps({"config": config, "profile": profile()}).encode()
    await messages.put({"type": "http.request", "body": body, "more_body": False})

    async def run(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    async def send(message):
        pass

    monkeypatch.setattr(setup, endpoint, run)
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "scheme": "http", "server": ("service.test", 80), "client": ("127.0.0.1", 1234), "method": "POST", "path": f"/v1/{endpoint}", "raw_path": f"/v1/{endpoint}".encode(), "query_string": b"", "root_path": "", "headers": [(b"authorization", b"Bearer t"), (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]}
    task = asyncio.create_task(create_app()(scope, messages.get, send))
    try:
        await asyncio.wait_for(started.wait(), 0.5)
        await messages.put({"type": "http.disconnect"})
        await asyncio.wait_for(task, 0.5)
        assert closed.is_set()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        svc.db.close()


@pytest.mark.parametrize("problem", ["missing", "malformed", "private"])
async def test_uninspected_nested_source_keeps_setup_coverage_unknown(monkeypatch, problem):
    good, bad = SITE + "good.xml", SITE + "bad.xml"
    sf = SyntheticSetup({INDEX: index([good, bad]), good: urlset(1), bad: b"" if problem == "missing" else b"<html>unavailable</html>"})
    if problem == "private":
        async def private(url):
            return url == bad
        monkeypatch.setattr("app.jobs.discovery.url_is_private", private)
    result = await setup._analyze(service(), {"discovery_mode": "auto", "sitemap_url": INDEX}, SITE, sf)
    assert result["sitemap"]["inspection"] == "limited"
    assert result["sitemap"]["url_count_capped"] is True
    assert result["sitemap"]["url_count"] == 1
    assert result["sitemap"]["target_share"] is None
    assert not schema_errors("analysis.schema.json", result)


async def test_sitemap_metadata_preserves_sanitized_browser_unavailable():
    sf = SyntheticSetup({})

    async def failed_fetch(url):
        raise setup.SetupBrowserUnavailable("TargetClosedError")

    sf.fetch = failed_fetch
    with pytest.raises(setup.SetupBrowserUnavailable) as error:
        await setup._analyze(service(), {"discovery_mode": "auto", "sitemap_url": INDEX}, SITE, sf)
    assert error.value.status == 503
    assert error.value.details["reason"] == "browser_unavailable"
