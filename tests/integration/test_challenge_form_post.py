"""A native challenge form can complete without authorizing ordinary forms."""
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import ssl
from threading import Thread

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import pytest

from app.fetching.oneoff import BrowserSession
from app.fetching.protection import RouteBudget
from app.fetching.destinations import DestinationDenied


@pytest.fixture
def challenge_document(tmp_path):
    hits = []
    options = {"status": 403, "cf_headers": True, "target": "/product", "form_type": "opaque", "redirect": None,
               "enctype": "application/x-www-form-urlencoded", "replay": False}
    class Handler(BaseHTTPRequestHandler):
        def version_string(self):
            return "cloudflare"

        def do_GET(self):
            hits.append(("GET", self.path))
            fields = "".join(f'<input type="hidden" name="{str(n) * 64}" value="opaque-proof-{n}">' for n in (1, 2, 3))
            if options["form_type"] == "ordinary":
                fields = '<input name="quantity" value="1">'
            if options["form_type"] == "duplicate":
                fields += f'<input type="hidden" name="{"1" * 64}" value="second">'
            body = ('<title>Just a moment...</title><script>window._cf_chl_opt = {};</script>'
                    f'<form method="post" enctype="{options["enctype"]}" action="{options["target"]}">{fields}</form>'
                    '<script>setTimeout(() => document.forms[0].submit(), 150);</script>').encode()
            self.send_response(options["status"])
            if options["cf_headers"]:
                self.send_header("cf-mitigated", "challenge")
                self.send_header("cf-ray", "fixture-ray")
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            hits.append(("POST", self.path))
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = b'<title>Product</title><main>Product ready</main>'
            if options["replay"]:
                body += ('<form method="post">' + "".join(f'<input type="hidden" name="{str(n) * 64}" value="proof-{n}">' for n in (1, 2, 3))
                         + '</form><script>setTimeout(() => document.forms[0].submit(), 150);</script>').encode()
            self.send_response(307 if options["redirect"] else 200)
            if options["redirect"]:
                self.send_header("Location", options["redirect"])
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_file, key_file = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{server.server_port}", options, hits
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
async def challenge_session(monkeypatch):
    from tests.unit.test_logic import settings
    monkeypatch.setattr("app.settings._settings", settings())
    async def public(*args, **kwargs):
        return False
    async def no_geo(*args, **kwargs):
        return None
    async def context(browser, *args, **kwargs):
        return await browser.new_context(service_workers="block", ignore_https_errors=True)
    monkeypatch.setattr("app.fetching.oneoff.url_is_private", public)
    monkeypatch.setattr("app.fetching.firefox_policy.url_is_private", public)
    monkeypatch.setattr("app.fetching.geo.exit_geo", no_geo)
    monkeypatch.setattr("app.fetching.identity.identity_for", no_geo)
    monkeypatch.setattr("app.fetching.identity.camoufox_context", context)
    budget = RouteBudget(counters={}, click_allowed=True, clicks_limit=2, wait_seconds=3,
                         click_wait_seconds=1, solver_skip="not_requested")
    session = BrowserSession("camoufox", timeout_s=10, budget=budget)
    try:
        await session.start()
        yield session
    finally:
        await session.close()


async def test_opted_in_native_challenge_form_completes(challenge_session, challenge_document):
    origin, _, hits = challenge_document
    try:
        status, html, final, _ = await challenge_session.get(origin + "/product")
    except Exception as exc:
        allowance = challenge_session._native_document_policy.guard.challenge_form
        pytest.fail(f"{type(exc).__name__}; native allowance: {vars(allowance)}")
    assert status == 200
    assert "Product ready" in html
    assert final == origin + "/product"
    assert [hit for hit in hits if hit[0] == "POST"] == [("POST", "/product")]


@pytest.mark.parametrize("change", [
    {"cf_headers": False}, {"status": 200}, {"form_type": "ordinary"}, {"form_type": "duplicate"},
    {"enctype": "multipart/form-data"}, {"enctype": "text/plain"}, {"target": "/cart?add-to-cart=1"},
])
async def test_unqualified_form_never_reaches_post_handler(challenge_session, challenge_document, change):
    origin, options, hits = challenge_document
    options.update(change)
    try:
        await challenge_session.get(origin + "/product")
    except DestinationDenied:
        pass
    assert not [hit for hit in hits if hit[0] == "POST"]


async def test_disabled_permission_never_reaches_post_handler(challenge_session, challenge_document):
    origin, _, hits = challenge_document
    challenge_session.budget.click_allowed = False
    with pytest.raises(DestinationDenied):
        await challenge_session.get(origin + "/product")
    assert not [hit for hit in hits if hit[0] == "POST"]


async def test_expired_permission_never_reaches_post_handler(challenge_session, challenge_document, monkeypatch):
    origin, _, hits = challenge_document
    monkeypatch.setattr("app.fetching.challenge_forms.FORM_TTL_SECONDS", 0.05)
    with pytest.raises(DestinationDenied):
        await challenge_session.get(origin + "/product")
    assert not [hit for hit in hits if hit[0] == "POST"]


async def test_caller_refusal_never_reaches_post_handler(challenge_session, challenge_document):
    origin, _, hits = challenge_document
    async def denied(url):
        return False
    with pytest.raises(DestinationDenied):
        await challenge_session.get(origin + "/product", destination_policy=denied)
    assert not [hit for hit in hits if hit[0] == "POST"]


@pytest.mark.parametrize("redirect", ["/other", "/product"])
async def test_post_preserving_redirect_has_no_second_post(challenge_session, challenge_document, redirect):
    origin, options, hits = challenge_document
    options["redirect"] = redirect
    try:
        await challenge_session.get(origin + "/product")
    except Exception:
        pass  # Native cancellation may surface as a transport or policy error.
    assert [hit for hit in hits if hit[0] == "POST"] == [("POST", "/product")]


async def test_native_post_replay_has_no_second_post(challenge_session, challenge_document):
    import asyncio
    origin, options, hits = challenge_document
    options["replay"] = True
    failed = asyncio.Event()
    challenge_session.page.on("requestfailed", lambda request: failed.set() if request.method == "POST" else None)
    try:
        await challenge_session.get(origin + "/product")
    except DestinationDenied:
        pass
    await asyncio.wait_for(failed.wait(), 3)
    assert [hit for hit in hits if hit[0] == "POST"] == [("POST", "/product")]
