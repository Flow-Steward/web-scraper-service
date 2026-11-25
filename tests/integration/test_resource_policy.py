"""Local proxy fixtures: no supplier, public network or paid proxy is used."""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import urlsplit

import pytest

from app.fetching.oneoff import BrowserSession
from tests.conftest import ensure_mappings, fast, merged_config, profile, start


@pytest.fixture
def resource_proxy():
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            target = urlsplit(self.path)
            hits.append((target.hostname, target.path))
            redirects = {"/script-redirect": "http://embed.tawk.to/redirected-widget.js",
                         "/frame-redirect": "http://embed.tawk.to/redirected-frame",
                         "/site-redirect": "http://tawk.to/product" if target.hostname == "catalogue.test" else "http://catalogue.test/product"}
            if target.path in redirects:
                self.send_response(302)
                self.send_header("Location", redirects[target.path])
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            kind = "text/javascript"
            if target.path == "/robots.txt":
                body, kind = b"User-agent: *\nAllow: /\n", "text/plain"
            elif target.path == "/product":
                body, kind = (b'''<!doctype html><html><head><title>Fixture product</title></head><body>
                <main class="product"><h1>Waiting</h1><span class="sku">R-1</span></main>
                <script src="http://cdn.catalogue.test/product.js"></script>
                <script src="http://challenges.cloudflare.com/resource.js"></script>
                <script src="http://embed.tawk.to/widget.js"></script>
                <script src="http://cdn-cookieyes.com/banner.js"></script>
                <script src="http://omnisnippet1.com/launcher.js"></script>
                <iframe src="http://embed.tawk.to/frame"></iframe>
                <script src="http://cdn.catalogue.test/script-redirect"></script>
                <iframe src="http://cdn.catalogue.test/frame-redirect"></iframe>
                <img src="http://catalogue.test/picture.png"></body></html>''', "text/html")
            elif target.path == "/product.js":
                body = b"document.querySelector('h1').textContent='External API product';"
            elif target.path == "/resource.js":
                body = b"window.fixtureChallengeDependency=true;"
            elif target.path == "/frame":
                body, kind = b"<p>Optional widget</p>", "text/html"
            elif target.path == "/picture.png":
                body, kind = b"", "image/png"
            else:
                body = b"window.fixtureWidgetLoaded=true;"
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, hits
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("browser", ["chromium", "camoufox"])
@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("site_host", ["catalogue.test", "tawk.to"])
async def test_setup_session_filters_widgets_before_upstream(browser, enabled, site_host, resource_proxy, monkeypatch):
    from tests.unit.test_logic import settings

    monkeypatch.setattr("app.settings._settings", settings())

    async def no_geo(*args, **kwargs):
        return None

    async def context(browser, *args, **kwargs):
        return await browser.new_context(proxy=kwargs.get("proxy"), service_workers="block")

    monkeypatch.setattr("app.fetching.geo.exit_geo", no_geo)
    monkeypatch.setattr("app.fetching.identity.identity_for", no_geo)
    monkeypatch.setattr("app.fetching.identity.chromium_context", context)
    monkeypatch.setattr("app.fetching.identity.camoufox_context", context)
    port, hits = resource_proxy
    session = BrowserSession(browser, proxy=f"http://127.0.0.1:{port}", timeout_s=15, block_heavy_resources=enabled)
    try:
        await session.start()
        status, html, final, _ = await session.get(f"http://{site_host}/product")
        assert status == 200 and "External API product" in html
        assert await session.page.evaluate("window.fixtureChallengeDependency") is True
    finally:
        await session.close()
    assert ("cdn.catalogue.test", "/product.js") in hits
    assert ("challenges.cloudflare.com", "/resource.js") in hits
    for resource in [("cdn-cookieyes.com", "/banner.js"), ("omnisnippet1.com", "/launcher.js")]:
        assert (resource in hits) is not enabled, hits
    for path in ["/widget.js", "/frame", "/redirected-widget.js", "/redirected-frame"]:
        assert (("embed.tawk.to", path) in hits) is (not enabled or site_host == "tawk.to"), hits
    assert ("cdn.catalogue.test", "/script-redirect") in hits
    assert ("cdn.catalogue.test", "/frame-redirect") in hits
    assert (("catalogue.test", "/picture.png") in hits) is not enabled


@pytest.mark.parametrize("browser", ["chromium", "camoufox"])
@pytest.mark.parametrize("enabled", [True, False])
def test_job_filters_widgets_and_preserves_external_product_data(service, browser, enabled, resource_proxy):
    port, hits = resource_proxy
    mapping = {"schema_version": 1, "template_id": "resource_product",
               "match": {"url_patterns": ["/product$"], "selector": "main.product"},
               "fields": [{"name": "title", "type": "text", "required": True,
                           "sources": [{"kind": "css", "selector": "h1", "extract": "text"}]}]}
    ensure_mappings(service, "catalogue.test", mapping)
    prof = profile(proxy_tiers=[{"tier": 1, "provider": "generic", "protocol": "http", "host": "127.0.0.1", "port": port,
                                 "auth_mode": "ip_allowlist", "session_mode": "rotating", "sticky_minutes": 10}])
    cfg = merged_config(service, {"discovery_mode": "url_list", "urls": [{"url": "http://catalogue.test/product"}],
                                  "fields": ["title"], "browser": browser, "fetch_mode": "browser", "respect_robots": False,
                                  "block_heavy_resources": enabled, **fast()}, prof=prof)
    response = start(service, cfg, f"resource-economy-{browser}-{enabled}", prof=prof)
    assert response.status_code == 201, response.text
    run = service.wait_run(response.json()["run_id"])
    assert run["outcome"] == "complete" and run["counters"]["records_extracted"] == 1, run
    records = [json.loads(line) for line in service.get(run["result"]["download_path"]).text.splitlines()]
    assert records[0]["data"]["title"] == "External API product"
    assert ("cdn.catalogue.test", "/product.js") in hits
    assert ("challenges.cloudflare.com", "/resource.js") in hits
    for resource in [("cdn-cookieyes.com", "/banner.js"), ("omnisnippet1.com", "/launcher.js")]:
        assert (resource in hits) is not enabled, hits
    for path in ["/widget.js", "/frame", "/redirected-widget.js", "/redirected-frame"]:
        assert (("embed.tawk.to", path) in hits) is not enabled, hits
    assert ("cdn.catalogue.test", "/script-redirect") in hits
    assert ("cdn.catalogue.test", "/frame-redirect") in hits
