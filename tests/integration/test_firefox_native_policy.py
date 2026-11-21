"""Fixture-only native Camoufox redirects: denied destinations receive zero requests."""
from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit

import pytest
from playwright.async_api import Error, async_playwright

from app.fetching.destinations import DestinationDenied, document_destination_allowed
from app.fetching.firefox_policy import FirefoxDocumentGuard


@pytest.fixture
def native_documents():
    hits = []
    redirects = {"/list": "/list/next", "/list/next": "/list/final"}
    documents = {}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append((self.path, dict(self.headers)))
            if self.path in redirects:
                self.send_response(302)
                self.send_header("Location", redirects[self.path])
                self.send_header("Set-Cookie", "native_cookie=kept; Path=/")
                self.end_headers()
                return
            body = documents.get(self.path, '<main>Fixture document</main><link rel="stylesheet" href="/assets/cart.css?add-to-cart=42">').encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/css" if self.path.startswith("/assets/") else "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_POST(self):
            # Record actual POST handling, independently of do_GET, so an unsafe
            # method escaping the native guard cannot disappear as an HTTP501.
            hits.append((self.path, {**dict(self.headers), "X-Fixture-Method": "POST"}))
            self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 16384))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", redirects, documents, hits
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.fixture
async def native_browser(monkeypatch):
    from app.fetching import firefox_policy, oneoff
    from app.fetching.browsers import launch_camoufox
    async def local_fixture_allowed(url):
        return urlsplit(url).path == "/private-frame"
    monkeypatch.setattr(firefox_policy, "url_is_private", local_fixture_allowed)
    monkeypatch.setattr(oneoff, "url_is_private", local_fixture_allowed)
    async with async_playwright() as playwright:
        browser = await launch_camoufox(playwright, headless=True)
        bridge = browser._wss_firefox_policy_bridge
        addon_path = Path(bridge.addon_path)
        try:
            yield browser
        finally:
            await browser.close()
            assert not addon_path.exists()
            assert not bridge._tasks and not bridge._writers and not bridge._tabs


async def guarded_page(browser, url, policy=None):
    from app.fetching.oneoff import private_route_guard
    context = await browser.new_context(service_workers="block")
    page = await context.new_page()
    guard = await FirefoxDocumentGuard(page, url, destination_policy=policy, timeout_s=5).start()
    await page.route("**/*", private_route_guard(None, guard.request))
    return page, guard


@pytest.mark.parametrize("reason", ["action", "scope", "robots", "cancelled"])
async def test_camoufox_native_denied_redirect_zero_target_hits(native_browser, native_documents, reason):
    origin, redirects, _, hits = native_documents
    target = {"action": "/cart?add-to-cart=42", "scope": "/other", "robots": "/list/blocked", "cancelled": "/list/final"}[reason]
    redirects["/list/next"] = target
    async def policy(url):
        import re
        return await document_destination_allowed(
            url, site_url=origin, rules={"sensitive_path", "action_params"},
            scope=[re.compile(r"/list(?:/|$)")], robots_allowed=lambda u: urlsplit(u).path != "/list/blocked",
            cancelled=lambda: reason == "cancelled" and len(hits) >= 2,
        )
    page, guard = await guarded_page(native_browser, origin + "/list", policy)
    try:
        with pytest.raises(Error):
            await page.goto(origin + "/list", timeout=10000)
        assert any(isinstance(exc, DestinationDenied) for exc in guard.errors)
        assert [path for path, _ in hits if not path.startswith("/assets/")] == ["/list", "/list/next"]
        assert target not in [path for path, _ in hits]
    finally:
        await guard.close()


async def test_camoufox_native_allowed_chain_retains_cookies_headers_resources(native_browser, native_documents):
    origin, _, _, hits = native_documents
    page, guard = await guarded_page(native_browser, origin + "/list")
    try:
        await page.context.set_extra_http_headers({"X-Fixture": "native-firefox"})
        response = await page.goto(origin + "/list", timeout=10000)
        assert response.status == 200 and page.url == origin + "/list/final"
        chain = [(path, headers) for path, headers in hits if path.startswith("/list")]
        assert [path for path, _ in chain] == ["/list", "/list/next", "/list/final"]
        assert all(headers.get("X-Fixture") == "native-firefox" for _, headers in chain)
        assert "native_cookie=kept" in chain[-1][1].get("Cookie", "")
        assert "/assets/cart.css?add-to-cart=42" in [path for path, _ in hits]
        assert guard.errors == []
    finally:
        await guard.close()


async def test_camoufox_native_iframe_public_allowed_private_redirect_zero_hits(native_browser, native_documents):
    origin, redirects, documents, hits = native_documents
    foreign = origin.replace("127.0.0.1", "localhost")
    documents["/list/frame-host"] = f'<iframe src="{foreign}/public-frame"></iframe><iframe src="/frame-redirect"></iframe>'
    redirects["/frame-redirect"] = "/private-frame"
    calls = []
    async def main_policy(url):
        calls.append(url)
        return True
    page, guard = await guarded_page(native_browser, origin + "/list/frame-host", main_policy)
    try:
        await page.goto(origin + "/list/frame-host", timeout=10000)
        paths = [path for path, _ in hits]
        assert "/public-frame" in paths and "/frame-redirect" in paths
        assert "/private-frame" not in paths
        assert calls == []
    finally:
        await guard.close()


async def test_camoufox_native_multiple_context_policies_remain_isolated(native_browser, native_documents):
    origin, redirects, _, hits = native_documents
    redirects["/list/a"] = "/list/a-final"
    redirects["/list/b"] = "/list/b-final"
    seen_a, seen_b = [], []
    async def policy_a(url):
        seen_a.append(url)
        return not url.endswith("a-final")
    async def policy_b(url):
        seen_b.append(url)
        return True
    a, ga = await guarded_page(native_browser, origin + "/list/a", policy_a)
    b, gb = await guarded_page(native_browser, origin + "/list/b", policy_b)
    try:
        results = await asyncio.gather(a.goto(origin + "/list/a"), b.goto(origin + "/list/b"), return_exceptions=True)
        assert isinstance(results[0], Error) and results[1].status == 200
        paths = [path for path, _ in hits]
        assert "/list/a-final" not in paths and "/list/b-final" in paths
        assert all("/list/a" in url for url in seen_a)
        assert all("/list/b" in url for url in seen_b)
        assert a.context is not b.context
    finally:
        await ga.close()
        await gb.close()


async def test_camoufox_native_unregistered_page_fails_closed(native_browser, native_documents):
    origin, _, _, hits = native_documents
    context = await native_browser.new_context(service_workers="block")
    page = await context.new_page()
    with pytest.raises(Error):
        await page.goto(origin + "/list", timeout=10000)
    assert hits == []
    await context.close()


@pytest.mark.parametrize("target", ["data:text/html,blocked", "javascript:alert(1)", "http://["])
async def test_camoufox_native_non_http_or_malformed_redirect_fails_closed(native_browser, native_documents, target):
    origin, redirects, _, hits = native_documents
    redirects["/list/scheme"] = target
    page, guard = await guarded_page(native_browser, origin + "/list/scheme")
    try:
        with pytest.raises(Error):
            await page.goto(origin + "/list/scheme", timeout=10000)
        assert [path for path, _ in hits] == ["/list/scheme"]
        assert not page.url.startswith(("data:", "javascript:"))
    finally:
        await guard.close()


async def test_camoufox_native_original_admission_not_rejected_by_route_restart(native_browser, native_documents):
    origin, _, _, hits = native_documents
    calls = []
    async def reject_new_destinations(url):
        calls.append(url)
        return False
    page, guard = await guarded_page(native_browser, origin + "/list/original", reject_new_destinations)
    try:
        response = await page.goto(origin + "/list/original", timeout=10000)
        assert response.status == 200
        assert "/list/original" in [path for path, _ in hits]
        assert calls == [] and guard.errors == []
    finally:
        await guard.close()


async def test_native_fixture_records_real_post_handler_hits(native_documents):
    import httpx
    origin, _, _, hits = native_documents
    async with httpx.AsyncClient(trust_env=False) as client:
        response = await client.post(origin + "/handler-control", data={"fixture": "control"})
    assert response.status_code == 200
    assert [(path, headers["X-Fixture-Method"]) for path, headers in hits] == [("/handler-control", "POST")]


@pytest.mark.parametrize("target_path", ["/list/form", "/list/update"])
async def test_camoufox_native_form_post_attempt_has_zero_target_post_hits(native_browser, native_documents, target_path):
    origin, _, documents, hits = native_documents
    documents["/list/form"] = '<main>Native GET form fixture</main>'
    page, guard = await guarded_page(native_browser, origin + "/list/form")
    try:
        response = await page.goto(origin + "/list/form", timeout=10000)
        assert response.status == 200
        target = origin + target_path
        async with page.expect_event("requestfailed", predicate=lambda request: request.method == "POST" and request.is_navigation_request(), timeout=5000) as failure:
            attempted = await page.evaluate('''async target => {
                const form = document.createElement("form");
                form.method = "POST";
                form.action = target;
                const input = document.createElement("input");
                input.name = "fixture"; input.value = "unsafe-method-control";
                form.append(input); document.body.append(form);
                await fetch("/submission-attempted");
                HTMLFormElement.prototype.submit.call(form);
                return true;
            }''', target)
        request = await failure.value
        assert attempted is True and request.url == target and request.method == "POST"
        assert "/submission-attempted" in [path for path, _ in hits]
        assert all(headers.get("X-Fixture-Method") != "POST" for _, headers in hits)
        assert any(isinstance(exc, DestinationDenied) for exc in guard.errors)
    finally:
        await guard.close()
