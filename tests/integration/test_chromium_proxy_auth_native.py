"""Real native Chromium proxy auth, including local TLS/CONNECT and origin401.

All sockets terminate in this test process. TLS trust is relaxed only in the test
context; no external proxy, supplier, or identity endpoint is used.
"""
import asyncio
import base64
import copy
import ssl
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from playwright.async_api import Error

from app.core.config import merge_layers
from app.core.urls import site_key
from app.fetching.oneoff import BrowserSession
from app.fetching.proxy_auth import ProxyAuthenticationRequired, is_proxy_auth_failure
from app.jobs.runner import JobRunner
from app.service import Service
from tests.conftest import platform, profile
from tests.unit.test_logic import settings

USER = "account-country-lv-city-riga-session-stickyfixture-duration-10"
PASSWORD = "fixture-native-password"


@pytest.fixture
async def local_proxy(tmp_path, monkeypatch):
    svc = Service(settings(data_dir=str(tmp_path / "state"), secrets_key="fixture-only-native-key-0123456789", supervisor_interval_seconds=0.01, headless=False))
    monkeypatch.setattr("app.settings._settings", svc.settings)
    async def no_geo(*args, **kwargs):
        return None
    monkeypatch.setattr("app.fetching.geo.exit_geo", no_geo)
    from app.fetching.identity import chromium_context
    async def test_context(*args, **kwargs):
        kwargs["ignore_https_errors"] = True  # self-signed loopback fixture only
        return await chromium_context(*args, **kwargs)
    monkeypatch.setattr("app.fetching.identity.chromium_context", test_context)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "shop.test")])
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(datetime.now(UTC) - timedelta(days=1))
            .not_valid_after(datetime.now(UTC) + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("shop.test")]), critical=False).sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert_path, key_path)
    state = {"origin": [], "proxy": [], "writers": set()}

    async def origin(reader, writer):
        state["writers"].add(writer)
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
            lines = raw.decode("latin1").split("\r\n")
            path = lines[0].split(" ")[1]
            headers = {k.lower(): v for line in lines[1:] if ": " in line for k, v in [line.split(": ", 1)]}
            state["origin"].append((path, headers))
            status = b"401 Unauthorized" if path.startswith("/auth") else b"200 OK"
            challenge = b'WWW-Authenticate: Basic realm="origin-fixture"\r\n' if status.startswith(b"401") else b""
            body = b'<main class="sheet"><h1>Native proxy fixture</h1><p>' + b"A complete local fixture description. " * 40 + b"</p></main>"
            writer.write(b"HTTP/1.1 " + status + b"\r\n" + challenge + b"Content-Type: text/html\r\nContent-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            state["writers"].discard(writer)

    plain = await asyncio.start_server(origin, "127.0.0.1", 0)
    secure = await asyncio.start_server(origin, "127.0.0.1", 0, ssl=tls)
    plain_port, tls_port = plain.sockets[0].getsockname()[1], secure.sockets[0].getsockname()[1]
    expected = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()

    async def relay(reader, writer):
        try:
            while chunk := await reader.read(65536):
                writer.write(chunk)
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()

    async def proxy(reader, writer):
        state["writers"].add(writer)
        upstream = None
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
            lines = raw.decode("latin1").split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            headers = {k.lower(): v for line in lines[1:] if ": " in line for k, v in [line.split(": ", 1)]}
            allowed = target == f"shop.test:{tls_port}" if method == "CONNECT" else target.startswith(f"http://shop.test:{plain_port}/")
            if not allowed:
                writer.write(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return  # Reject browser background traffic locally.
            authenticated = headers.get("proxy-authorization") == expected
            credential = headers.get("proxy-authorization", "")
            try:
                supplied_user = base64.b64decode(credential.split(" ", 1)[1]).decode().split(":", 1)[0]
            except (ValueError, IndexError, UnicodeError):
                supplied_user = None
            state["proxy"].append({"method": method, "authenticated": authenticated,
                                   "credential_present": bool(credential), "username_matches": supplied_user == USER})
            if not authenticated:
                writer.write(b'HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm="proxy-fixture"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                await writer.drain()
                return
            port = tls_port if method == "CONNECT" else plain_port
            upstream_reader, upstream = await asyncio.open_connection("127.0.0.1", port)
            state["writers"].add(upstream)
            if method == "CONNECT":
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
            else:
                path = urlsplit(target).path or "/"
                forwarded = [line for line in lines[1:] if line and not line.lower().startswith(("proxy-authorization:", "proxy-connection:", "connection:"))]
                upstream.write((f"GET {path} HTTP/1.1\r\n" + "\r\n".join(forwarded) + "\r\nConnection: close\r\n\r\n").encode("latin1"))
                await upstream.drain()
            await asyncio.gather(relay(reader, upstream), relay(upstream_reader, writer))
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            state["writers"].discard(writer)
            if upstream:
                upstream.close()
                state["writers"].discard(upstream)

    gateway = await asyncio.start_server(proxy, "127.0.0.1", 0)
    state.update(svc=svc, port=gateway.sockets[0].getsockname()[1], http=f"http://shop.test:{plain_port}", https=f"https://shop.test:{tls_port}")
    try:
        yield state
    finally:
        await svc.jobs.shutdown()
        for server in (gateway, plain, secure):
            server.close()
            await server.wait_closed()
        for writer in list(state["writers"]):
            writer.close()
        svc.db.close()


@pytest.mark.parametrize("transport", ["http", "https"])
@pytest.mark.parametrize("outcome", ["good", "rejected", "origin401"])
async def test_native_proxy_credentials_and_origin_auth_are_separate(local_proxy, transport, outcome):
    fixture = local_proxy
    password = "fixture-wrong-password" if outcome == "rejected" else PASSWORD
    session = BrowserSession("chromium", proxy=f"http://{USER}:{password}@127.0.0.1:{fixture['port']}", timeout_s=15)
    try:
        await session.start()
        target = fixture[transport] + ("/auth" if outcome == "origin401" else "/product")
        if outcome == "rejected":
            with pytest.raises(ProxyAuthenticationRequired):
                await session.get(target)
            assert not fixture["origin"]
            assert any(item["credential_present"] and item["username_matches"] for item in fixture["proxy"])
        elif outcome == "good":
            status, html, _, _ = await session.get(target)
            assert status == 200 and "Native proxy fixture" in html
            assert any(item["authenticated"] for item in fixture["proxy"])
        else:
            try:
                status, _, _, _ = await session.get(target)
                assert status == 401
            except Error as exc:
                # Native headless Default may decline the origin auth prompt.
                assert not is_proxy_auth_failure(exc, proxy_used=True)
            assert not session._native_document_policy.errors
            assert any(path.startswith("/auth") for path, _ in fixture["origin"])
            assert any(item["authenticated"] for item in fixture["proxy"])
        assert all("authorization" not in headers and "proxy-authorization" not in headers for _, headers in fixture["origin"])
        if transport == "https":
            assert all(item["method"] == "CONNECT" for item in fixture["proxy"])
    finally:
        await session.close()


async def test_connect_rejection_pauses_job_and_approved_resume_collects_once(local_proxy):
    fixture, svc = local_proxy, local_proxy["svc"]
    target = fixture["https"] + "/product"
    tier = {"tier": 1, "provider": "generic", "protocol": "http", "host": "127.0.0.1", "port": fixture["port"],
            "auth_mode": "user_password", "username": USER, "password": "fixture-wrong-password"}
    config, _ = merge_layers({"discovery_mode": "url_list", "urls": [{"url": target}], "fetch_mode": "browser", "browser": "chromium",
        "fields": ["title"], "max_concurrency": 1, "max_retries": 0, "delay_min_seconds": 0, "delay_max_seconds": 0,
        "wait_for_selector_ms": 0, "page_timeout_seconds": 15}, {}, {})
    svc.mappings.activate(site_key(target), "sheet", 0, {"schema_version": 1, "template_id": "sheet", "match": {"selector": ".sheet"},
        "fields": [{"name": "title", "type": "text", "required": True, "sources": [{"kind": "css", "selector": "h1", "extract": "text"}]}]}, None)
    _, summary = await svc.jobs.create({"config": config, "profile": profile(proxy_tiers=[tier]), "platform": platform("native-connect")}, "native-connect")
    run_id = summary["run_id"]
    svc.jobs._set_state(run_id, "running")
    runner = JobRunner(svc, run_id)
    runner.config["respect_robots"] = False
    runner._persist()
    svc.jobs.runners[run_id] = runner
    runner.start()
    await asyncio.wait_for(runner.task, 30)
    assert svc.jobs.summary(run_id)["state"] == "waiting_for_credentials"
    assert svc.db.scalar("SELECT COUNT(*) FROM unextracted WHERE run_id=?", (run_id,)) == 0
    assert not fixture["origin"]
    assert any(item["credential_present"] and item["username_matches"] for item in fixture["proxy"])
    tier = copy.deepcopy(tier)
    tier["password"] = PASSWORD
    await svc.jobs.resume(run_id, profile(proxy_tiers=[tier]))
    resumed = svc.jobs.runners[run_id]
    await asyncio.wait_for(resumed.task, 30)
    assert svc.jobs.summary(run_id)["state"] == "completed"
    assert svc.db.scalar("SELECT COUNT(*) FROM records WHERE run_id=?", (run_id,)) == 1
    assert sum(path == "/product" for path, _ in fixture["origin"]) == 1
    assert all(item["method"] == "CONNECT" for item in fixture["proxy"])
