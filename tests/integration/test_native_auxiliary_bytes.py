"""A cleared browser document can differ from the context HTTP client's reply."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace
import asyncio

import pytest

from app.fetching.protection import RouteBudget
from app.jobs.runner import JobRunner
from tests.integration.test_native_browser_session import native_session

XML = b'<?xml version="1.0"?><urlset><url><loc>https://catalogue.test/product/1</loc></url></urlset>'


@pytest.fixture
def browser_only_sitemap():
    hits = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            native = self.headers.get("Sec-Fetch-Dest") == "document"
            hits.append((self.path, native))
            cleared = "clearance=fixture" in self.headers.get("Cookie", "")
            if native and cleared:
                status, kind, body = 200, "application/xml", XML
            else:
                status, kind = 403, "text/html"
                body = (b'<title>Just a moment...</title><div id="cf-wrapper">Checking your browser</div>'
                        b'<script>window.__cf_chl_opt={};setTimeout(function(){'
                        b'document.cookie="clearance=fixture;path=/";location.reload();},750);</script>')
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/sitemap.xml", hits
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
async def cleared_session(native_session, monkeypatch):
    async def public(*args, **kwargs):
        return False
    monkeypatch.setattr("app.fetching.oneoff.url_is_private", public)
    monkeypatch.setattr("app.fetching.firefox_policy.url_is_private", public)
    native_session.budget = RouteBudget(counters={}, wait_seconds=3)
    return native_session


async def test_setup_raw_keeps_native_xml_after_clearance(cleared_session, browser_only_sitemap):
    url, hits = browser_only_sitemap
    result = await cleared_session.raw(url)
    assert result.status == 200 and result.body == XML
    assert result.headers["content-type"] == "application/xml"
    assert sum(not native for _, native in hits) == 1


@pytest.mark.parametrize("original_status", [200, 403])
async def test_runtime_aux_reads_the_loaded_native_document(cleared_session, browser_only_sitemap, original_status):
    url, hits = browser_only_sitemap
    await cleared_session.get(url)
    results = []
    runner = SimpleNamespace(config={}, state="running", tiers=SimpleNamespace(enabled=False, can_escalate=lambda host: False),
        _shutdown=False, _cut_or_stopping=lambda: False,
        prot=lambda: {"hard_blocks": 0}, _retire=lambda ctx: None,
        _aux_done=lambda request, result: results.append(result))
    ctx = SimpleNamespace(request=SimpleNamespace(url=url), page=cleared_session.page)
    await JobRunner._handle_aux(runner, ctx, "127.0.0.1", original_status, True)
    assert results[0].status == 200 and results[0].body == XML
    assert all(native for _, native in hits)


async def test_automatic_reload_before_detection_uses_current_status(cleared_session, browser_only_sitemap, monkeypatch):
    from app.fetching import challenge

    original_settle = challenge.settle
    async def delayed_detection(*args, **kwargs):
        loaded = await original_settle(*args, **kwargs)
        await asyncio.sleep(1.2)  # clearance reload completes before the handler gets CPU
        return loaded
    monkeypatch.setattr(challenge, "settle", delayed_detection)
    url, _ = browser_only_sitemap
    status, _, final, _ = await cleared_session.get(url)
    assert status == 200 and final == url
