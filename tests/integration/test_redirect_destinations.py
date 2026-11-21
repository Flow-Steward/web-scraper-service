"""Invented local documents prove redirect policy before target network issuance.

Run in the canonical image for both browser engines; no live target or proxy.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import urlsplit

import pytest

from app.fetching.destinations import document_destination_allowed


@pytest.fixture
def redirect_documents():
    hits = []
    redirects = {"/list?page=2": "/list/next?page=3", "/list/next?page=3": "/cart?add-to-cart=42"}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append((self.path, dict(self.headers)))
            if self.path in redirects:
                self.send_response(302)
                self.send_header("Location", redirects[self.path])
                self.send_header("Set-Cookie", "redirect_cookie=kept; Path=/")
                self.end_headers()
                return
            if self.path.startswith("/assets/"):
                body, kind = b"body {color: black}", "text/css"
            else:
                body, kind = (b'<link rel="stylesheet" href="/assets/cart.css?add-to-cart=42">'
                              b'<main>Allowed listing</main>', "text/html")
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", redirects, hits
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.mark.parametrize("reason", ["action", "scope", "robots", "cancelled"])
async def test_http_redirect_fixture_denied_target_zero_hits(redirect_documents, reason):
    import re
    from app.fetching.oneoff import http_get

    origin, redirects, hits = redirect_documents
    target = {"action": "/cart?add-to-cart=42", "scope": "/other",
              "robots": "/list/blocked", "cancelled": "/list/final"}[reason]
    redirects["/list/next?page=3"] = target

    async def allowed(url):
        return await document_destination_allowed(
            url, site_url=origin, rules={"sensitive_path", "action_params"},
            scope=[re.compile(r"/list(?:/|\?|$)")],
            robots_allowed=lambda u: urlsplit(u).path != "/list/blocked",
            cancelled=lambda: reason == "cancelled" and len(hits) >= 2,
        )

    result = await http_get(origin + "/list?page=2", check_private=False, destination_policy=allowed)
    assert result.error == "redirect_policy_denied"
    assert [path for path, _ in hits] == ["/list?page=2", "/list/next?page=3"]
    assert target not in [path for path, _ in hits]


async def test_http_allowed_redirect_fixture_keeps_identity_cookies_headers(redirect_documents):
    from app.fetching.oneoff import http_get

    origin, redirects, hits = redirect_documents
    redirects["/list/next?page=3"] = "/list/final?edition=mono"
    result = await http_get(origin + "/list?page=2", check_private=False, headers={"X-Fixture": "retained"})
    assert result.status == 200 and result.error is None
    assert result.final_url == origin + "/list/final?edition=mono"
    assert [path for path, _ in hits] == ["/list?page=2", "/list/next?page=3", "/list/final?edition=mono"]
    assert all(headers.get("X-Fixture") == "retained" for _, headers in hits)
    assert "redirect_cookie=kept" in hits[2][1].get("Cookie", "")


@pytest.fixture
def authenticated_robots_redirects():
    """A robots endpoint whose response depends on the configured caller header."""
    hits = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append((self.path, dict(self.headers)))
            if self.path in {"/list", "/list/next"}:
                self.send_response(302)
                self.send_header("Location", "/list/next" if self.path == "/list" else "/list/final")
                self.end_headers()
                return
            if self.path == "/robots.txt":
                authorized = self.headers.get("X-Robots-Key") == "retained"
                status = 200 if authorized else 401
                body = b"User-agent: *\nDisallow: /list/final\n" if authorized else b"Authentication required"
            else:
                status, body = 200, b"<main>Fixture document</main>"
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Type", "text/plain" if self.path == "/robots.txt" else "text/html")
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", hits
    server.shutdown()
    server.server_close()
    thread.join()
