"""Private redirects from an already committed out-of-process iframe are guarded."""
from __future__ import annotations

import os
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from urllib.parse import urlsplit

import pytest
from playwright.async_api import Route, async_playwright

from app.fetching.chromium_destinations import ChromiumDocumentGuard
from app.fetching.oneoff import PrivateAddress, private_route_guard


@pytest.fixture
def committed_iframe_documents():
    hits = []
    signals = {path: Event() for path in ("/clicked", "/iframe-redirect", "/private-target")}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append((self.headers["Host"], self.path))
            if self.path in signals:
                signals[self.path].set()
            port = self.server.server_port
            if self.path == "/iframe-redirect":
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{port}/private-target")
                self.end_headers()
                return
            if self.path == "/list":
                body = f'<iframe src="http://localhost:{port}/committed-frame"></iframe>'.encode()
            elif self.path == "/committed-frame":
                body = (b'<main id="committed">Committed third-party frame</main>'
                        b'<button id="go" onclick="(async () => { window.fixtureClicked = true; '
                        b'await fetch(\'/clicked\', {cache: \'no-store\'}); '
                        b'location.assign(\'/iframe-redirect\'); })()">Go</button>')
            else:
                body = b"<main>Denied private destination</main>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_port, hits, signals
    server.shutdown()
    server.server_close()
    thread.join()


async def test_committed_cross_site_iframe_private_redirect_has_zero_target_hits(committed_iframe_documents, monkeypatch):
    from tests.unit.test_logic import settings
    monkeypatch.setattr("app.settings._settings", settings())
    port, hits, signals = committed_iframe_documents
    origin = f"http://127.0.0.1:{port}"
    child_url = f"http://localhost:{port}/committed-frame"
    async def is_private(url):
        parts = urlsplit(url)
        return parts.hostname == "127.0.0.1" and parts.path == "/private-target"
    async def no_api_fetch(*args, **kwargs):
        raise AssertionError("Document traffic must remain native")
    monkeypatch.setattr("app.fetching.oneoff.url_is_private", is_private)
    monkeypatch.setattr("app.fetching.chromium_destinations.url_is_private", is_private)
    monkeypatch.setattr(Route, "fetch", no_api_fetch)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chromium", headless=True,
            args=["--no-sandbox", "--site-per-process"],
            executable_path=os.environ.get("WSS_TEST_CHROMIUM_PATH"))
        page = await browser.new_page(service_workers="block")
        guard = await ChromiumDocumentGuard(page, origin + "/list").start()
        await page.route("**/*", private_route_guard(None, guard.request))
        observer = await browser.new_browser_cdp_session()
        try:
            await page.goto(origin + "/list", timeout=5000)
            child = page.frame(url=child_url)
            assert child is not None
            assert await child.locator("#committed").inner_text() == "Committed third-party frame"
            targets = (await observer.send("Target.getTargets"))["targetInfos"]
            iframe_targets = [target for target in targets if target["type"] == "iframe" and target["url"] == child_url]
            assert len(iframe_targets) == 1, "Fixture must commit a distinct out-of-process iframe target"
            page_target = (await guard.session.send("Target.getTargetInfo"))["targetInfo"]["targetId"]
            assert iframe_targets[0]["targetId"] != page_target
            # Invoke the actual fixture button listener without Playwright's
            # navigation wait racing an iframe process swap. Unexpected evaluate
            # errors remain visible; the server marker proves the click ran.
            await child.evaluate("document.querySelector('#go').click()")
            assert await asyncio.to_thread(signals["/clicked"].wait, 3), hits
            assert await asyncio.to_thread(signals["/iframe-redirect"].wait, 3), (hits, guard.errors)
            async with asyncio.timeout(3):
                while not signals["/private-target"].is_set() and not guard.errors:
                    await asyncio.sleep(0.01)
            assert not any(path == "/private-target" for _, path in hits), hits
            assert any(isinstance(error, PrivateAddress) for error in guard.errors)
        finally:
            await observer.detach()
            await guard.close()
            await browser.close()
