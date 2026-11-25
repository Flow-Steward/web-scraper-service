"""A solver callback and a default form submission must not race each other."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import time

import pytest

from app.fetching.protection import RouteBudget
from tests.integration.test_native_browser_session import native_session


@pytest.fixture
def solver_gate():
    hits, options = [], {"callback": True}
    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, body, cleared=False):
            self.send_response(status)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            if cleared:
                self.send_header("Set-Cookie", "clearance=fixture; Path=/")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            hits.append(("GET", self.path))
            fallback = not options["callback"] and "cf-turnstile-response=fixture-token" in self.path
            if "clearance=fixture" in self.headers.get("Cookie", "") or fallback:
                self.reply(200, b"<title>Product</title><main>Product ready</main>", cleared=True)
                return
            callback = ("<script>window.turnstileCallback=function(t){fetch('/verify?token='+t,"
                        "{method:'POST',credentials:'include'}).then(function(){location.reload();});};</script>") if options["callback"] else ""
            body = ("<title>Just a moment...</title><div id='cf-wrapper'>Checking your browser</div>"
                    "<div class='cf-turnstile' data-sitekey='fixture-key'></div>"
                    "<form><input name='cf-turnstile-response'></form>"
                    "<script>window.__cf_chl_opt={};</script>" + callback).encode()
            self.reply(403, body)

        def do_POST(self):
            hits.append(("POST", self.path))
            time.sleep(.5)  # verification is pending when a competing form navigation starts
            self.reply(200, b"verified", cleared=True)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/product", hits, options
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("callback", [True, False])
async def test_solver_delivers_token_through_one_completion_path(native_session, solver_gate, monkeypatch, callback):
    async def public(*args, **kwargs):
        return False
    monkeypatch.setattr("app.fetching.oneoff.url_is_private", public)
    monkeypatch.setattr("app.fetching.firefox_policy.url_is_private", public)
    monkeypatch.setattr("app.fetching.chromium_destinations.url_is_private", public)
    class FixtureSolver:
        async def solve_turnstile(self, url, sitekey):
            return "fixture-token"
    native_session.budget = RouteBudget(counters={}, solver=FixtureSolver(), solves_limit=1, wait_seconds=0)
    url, hits, options = solver_gate
    options["callback"] = callback
    status, html, _, step = await native_session.get(url)
    assert status == 200 and "Product ready" in html and step == "solver", (status, hits, native_session.traces)
    if callback:
        assert [p for method, p in hits if method == "POST"] == ["/verify?token=fixture-token"]
        assert not any("cf-turnstile-response=" in path for _, path in hits)
    else:
        assert not any(method == "POST" for method, _ in hits)
        assert sum("cf-turnstile-response=fixture-token" in path for _, path in hits) == 1
