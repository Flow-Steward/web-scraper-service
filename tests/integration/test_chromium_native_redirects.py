"""Isolated CDP prototype: native Chromium networking, no API fetch substitute."""
from __future__ import annotations

import os
import re

import pytest
import pytest_asyncio
from playwright.async_api import Route, async_playwright

from app.fetching.chromium_destinations import ChromiumDocumentGuard
from app.fetching.destinations import document_destination_allowed
from app.fetching.oneoff import private_route_guard
from tests.integration.test_redirect_destinations import redirect_documents


@pytest_asyncio.fixture
async def native_page(monkeypatch):
    from tests.unit.test_logic import settings
    monkeypatch.setattr("app.settings._settings", settings())
    async def forbidden(*args, **kwargs):
        raise AssertionError("Native browser guard must never call Route.fetch")
    monkeypatch.setattr(Route, "fetch", forbidden)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chromium", headless=True,
            args=["--no-sandbox"], executable_path=os.environ.get("WSS_TEST_CHROMIUM_PATH"))
        page = await browser.new_page(service_workers="block")
        await page.set_extra_http_headers({"X-Fixture": "retained"})
        yield page
        await browser.close()


@pytest.mark.parametrize("reason", ["action", "encoded", "scope", "robots", "cancelled"])
async def test_native_cdp_redirect_denied_target_zero_hits(native_page, redirect_documents, reason):
    origin, redirects, hits = redirect_documents
    target = {"action": "/cart?add-to-cart=42", "encoded": "/%63art", "scope": "/other",
              "robots": "/list/blocked", "cancelled": "/list/final"}[reason]
    redirects["/list/next?page=3"] = target
    async def allowed(url):
        return await document_destination_allowed(
            url, site_url=origin, rules={"sensitive_path", "action_params"},
            scope=[re.compile(r"/list(?:/|\?|$)")],
            robots_allowed=lambda u: not u.endswith("/blocked"),
            cancelled=lambda: reason == "cancelled" and len(hits) >= 2,
        )
    guard = await ChromiumDocumentGuard(native_page, origin + "/list?page=2", destination_policy=allowed).start()
    await native_page.route("**/*", private_route_guard(None, guard.request))
    try:
        with pytest.raises(Exception):
            await native_page.goto(origin + "/list?page=2", timeout=5000)
        assert [path for path, _ in hits] == ["/list?page=2", "/list/next?page=3"]
        assert target not in [path for path, _ in hits]
        assert guard.errors
    finally:
        await guard.close()


async def test_native_cdp_allowed_identity_cookies_headers_static_resources(native_page, redirect_documents):
    origin, redirects, hits = redirect_documents
    redirects["/list/next?page=3"] = "/list/final?edition=mono"
    guard = await ChromiumDocumentGuard(native_page, origin + "/list?page=2").start()
    await native_page.route("**/*", private_route_guard(None, guard.request))
    try:
        response = await native_page.goto(origin + "/list?page=2", timeout=5000)
        assert response.status == 200
        assert native_page.url == origin + "/list/final?edition=mono"
        assert await native_page.locator("main").inner_text() == "Allowed listing"
        assert [path for path, _ in hits] == ["/list?page=2", "/list/next?page=3", "/list/final?edition=mono", "/assets/cart.css?add-to-cart=42"]
        assert all(headers.get("X-Fixture") == "retained" for _, headers in hits)
        assert "redirect_cookie=kept" in hits[2][1].get("Cookie", "")
        assert guard.errors == []
    finally:
        await guard.close()


@pytest.mark.parametrize("private_frame", [False, True])
async def test_native_cdp_public_challenge_frame_and_private_redirect_boundary(native_page, monkeypatch, private_frame):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from urllib.parse import urlsplit

    hits = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append((self.headers["Host"], self.path))
            if self.path == "/frame/start":
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{self.server.server_port}/frame/final")
                self.end_headers()
                return
            body = (b'<iframe src="/frame/start"></iframe>' if self.path == "/list"
                    else b'<main>Public challenge frame</main>')
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
    origin = f"http://127.0.0.1:{server.server_port}"
    async def is_private(url):
        return private_frame and urlsplit(url).hostname == "localhost"
    monkeypatch.setattr("app.fetching.oneoff.url_is_private", is_private)
    monkeypatch.setattr("app.fetching.chromium_destinations.url_is_private", is_private)
    guard = await ChromiumDocumentGuard(native_page, origin + "/list", destination_policy=lambda u: False).start()
    await native_page.route("**/*", private_route_guard(None, guard.request))
    try:
        response = await native_page.goto(origin + "/list", timeout=5000)
        assert response.status == 200
        final_hits = [path for _, path in hits if path == "/frame/final"]
        assert len(final_hits) == (0 if private_frame else 1)
        if private_frame:
            from app.fetching.oneoff import PrivateAddress
            assert isinstance(guard.errors[0], PrivateAddress)
        else:
            assert guard.errors == []
    finally:
        await guard.close()
        server.shutdown()
        server.server_close()
        thread.join()
