"""Unit tests for the review fixes (service review, 2026-11-13): transactions, detection, sessions, redaction, private addresses, delivery, gunzip, keys."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import hmac
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.settings import LIMIT_DEFAULTS, Settings

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixture"))
from cloudflare_pages import (  # noqa: E402
    block_1020_page, decorate_normal_page, managed_challenge_page, managed_challenge_rendered,
)


def settings(**kw):
    s = Settings(api_token="t", data_dir="/tmp", secrets_key=None, limits={k: v[0] for k, v in LIMIT_DEFAULTS.items()})
    for k, v in kw.items():
        setattr(s, k, v)
    return s


@pytest.fixture(autouse=True)
def _settings():
    from app import settings as mod
    from app.core import netguard

    mod.set_settings(settings())
    netguard._cache.clear()
    yield
    mod.set_settings(None)
    netguard._cache.clear()


# ---------------------------------------------------------------- no transaction spans an await
def test_a_second_task_cannot_join_an_open_transaction():
    """Before the fix task B's write joined A's open transaction and A's rollback removed it
    after B had been acknowledged. Now B is refused instead of being silently rolled back."""
    from app.core.db import Database, TransactionSpansAwait

    db = Database(Path(tempfile.mkdtemp()) / "t.sqlite3")
    acknowledged: list[str] = []

    async def task_a():
        try:
            with db.tx():
                db.execute("INSERT INTO meta(k, v) VALUES ('a', '1')")
                await asyncio.sleep(0.05)  # the forbidden await inside a transaction
                raise RuntimeError("A fails")
        except RuntimeError:
            pass

    async def task_b():
        await asyncio.sleep(0.01)
        try:
            with db.tx():
                db.execute("INSERT INTO meta(k, v) VALUES ('b', '1')")
            acknowledged.append("b")
        except TransactionSpansAwait:
            pass

    async def main():
        await asyncio.gather(task_a(), task_b())

    asyncio.run(main())
    rows = {r["k"] for r in db.all("SELECT k FROM meta")}
    # every acknowledged write is committed; nothing of A's failed transaction is
    assert set(acknowledged) <= rows and "a" not in rows


def test_sequential_transactions_of_two_tasks_both_commit():
    from app.core.db import Database

    db = Database(Path(tempfile.mkdtemp()) / "t.sqlite3")

    async def writer(k):
        for i in range(20):
            with db.tx():
                db.execute("INSERT INTO meta(k, v) VALUES (?, ?)", (f"{k}{i}", "x"))
            await asyncio.sleep(0)

    async def main():
        await asyncio.gather(writer("a"), writer("b"))

    asyncio.run(main())
    assert db.scalar("SELECT COUNT(*) FROM meta") == 40


# ---------------------------------------------------------------- detection on real-shaped pages
PRODUCT = ("<!doctype html><html><head><title>Pendelleuchte</title></head><body><div class='product-detail'><h1>Pendelleuchte</h1>"
           "<div class='product-description'>" + "<p>Mundgeblasenes Opalglas, E27, max. 60 W.</p>" * 40 + "</div></div></body></html>")
SMALL_PRODUCT = ("<!doctype html><html><head><title>Adapter</title></head><body><div class='product-detail'><h1>Adapter X</h1>"
                 "<div class='product-description'><p>Kurz.</p></div></div></body></html>")


@pytest.mark.parametrize("html,status", [
    (decorate_normal_page(PRODUCT), 200),        # JSD script + contact Turnstile + reCAPTCHA + "access denied" text
    (decorate_normal_page(SMALL_PRODUCT), 200),  # the same on a small page
    (PRODUCT.replace("</body>", "<script src='/cdn-cgi/challenge-platform/scripts/jsd/main.js'></script></body>"), 200),
    (SMALL_PRODUCT.replace("Kurz.", "Access denied for unsupported drivers. Request blocked by the firmware."), 200),
])
def test_normal_cloudflare_pages_are_not_challenges(html, status):
    from app.fetching.challenge import detect_html

    assert detect_html(html, status).kind == "none"


@pytest.mark.parametrize("html,status,kind", [
    (managed_challenge_page("shop.example"), 403, "challenge"),
    (managed_challenge_page("shop.example"), 503, "challenge"),
    (managed_challenge_rendered("shop.example"), None, "challenge"),   # in the browser, no status
    (managed_challenge_rendered("shop.example"), 200, "challenge"),
    (block_1020_page("shop.example"), 403, "hard_block"),
    (block_1020_page("shop.example"), None, "hard_block"),
])
def test_real_interstitials_and_blocks_are_detected(html, status, kind):
    from app.fetching.challenge import detect_html

    assert detect_html(html, status).kind == kind


def test_managed_turnstile_on_empty_page_is_a_challenge_but_not_in_a_form():
    from app.fetching.challenge import detect_html

    empty = ("<html><head><title>shop</title></head><body><div class='cf-turnstile' data-sitekey='0x4AAAA'></div>"
             "<iframe src='https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/if/ov2/x'></iframe></body></html>")
    assert detect_html(empty, 200).kind == "challenge"
    assert detect_html(decorate_normal_page(PRODUCT), 200).kind == "none"


# ---------------------------------------------------------------- redaction
@pytest.mark.parametrize("text,secret", [
    ("ProxyError: cannot connect to socks5h://alice:s3cretpw@gw.example:1080", "s3cretpw"),
    ("failed via socks4://bob:pw12345@10.0.0.1:1080 (timeout)", "pw12345"),
    ("tunnel http://onlyuser9@proxy.example:8080 refused", "onlyuser9"),
    ("{'server': 'http://p:8080', 'username': 'carol', 'password': 'hunter22'}", "hunter22"),
    ("{'server': 'http://p:8080', 'username': 'carol', 'password': 'hunter22'}", "carol"),
    ('{"clientKey": "CAP-0123456789"}', "CAP-0123456789"),
    ("Proxy-Authorization: Basic YWxpY2U6czNjcmV0", "YWxpY2U6czNjcmV0"),
    ("https://user:pa%40ss@host/x", "pa%40ss"),
])
def test_redaction_patterns(text, secret):
    from app.core.redaction import redact

    assert secret not in redact(text)


def test_redaction_of_exact_job_secret_values():
    from app.core.redaction import redact, secret_values

    secret = {"proxy_tiers": [{"username": "customer-acme", "password": "pw-xyz-123", "proxy_urls": ["http://u1:p1secret@h:1"]}],
              "solver": {"api_key": "SOLVERKEY42"}}
    text = "geo lookup for pw-xyz-123 failed; key SOLVERKEY42; user customer-acme; p1secret"
    out = redact(text, secret_values(secret))
    for value in ("pw-xyz-123", "SOLVERKEY42", "customer-acme", "p1secret"):
        assert value not in out


# ---------------------------------------------------------------- special ranges, unresolvable hosts
@pytest.mark.parametrize("addr", [
    "100.64.0.1", "100.100.100.200", "169.254.169.254", "10.1.2.3", "127.0.0.1", "0.0.0.0", "192.0.0.8", "198.18.0.1",
    "240.0.0.1", "224.0.0.1", "::1", "fe80::1", "fc00::1", "64:ff9b::a9fe:a9fe", "64:ff9b::7f00:1", "64:ff9b:1::1",
    "::ffff:169.254.169.254", "2002:a9fe:a9fe::1", "2001:0:4136:e378:8000:63bf:3f57:fdf2", "2001:db8::1",
])
def test_special_addresses_are_refused(addr):
    from app.core.netguard import ip_is_refused

    assert ip_is_refused(addr)


@pytest.mark.parametrize("addr", ["93.184.215.14", "8.8.8.8", "2606:4700:4700::1111", "64:ff9b::808:808"])
def test_public_addresses_pass(addr):
    from app.core.netguard import ip_is_refused

    assert not ip_is_refused(addr)


def test_unresolvable_and_denied_hosts_are_refused():
    from app import settings as mod
    from app.core.netguard import url_is_private

    assert asyncio.run(url_is_private("http://no-such-host.invalid/x")) is True
    mod.set_settings(settings(allow_private_targets=True, denied_target_hosts=["internal.test"]))
    assert asyncio.run(url_is_private("http://internal.test:8601/x")) is True
    assert asyncio.run(url_is_private("http://shop.test:8601/x")) is False


def test_guarded_transport_refuses_at_connect_time():
    """The check happens where the connection is made: a name that resolves to a private
    address is refused by the transport itself (no second, unchecked resolution)."""
    import httpx

    from app.core.netguard import PrivateAddressError, guarded_transport

    async def main():
        async with httpx.AsyncClient(transport=guarded_transport()) as client:
            await client.get("http://localhost:9/")

    with pytest.raises(PrivateAddressError):
        asyncio.run(main())


# ---------------------------------------------------------------- session lifetime
def test_session_lifetime_follows_sticky_minutes():
    from app.fetching.proxies import TierPlan
    from app.jobs.runner import JobRunner

    r = JobRunner.__new__(JobRunner)
    r.tiers = TierPlan([{"tier": 1, "session_mode": "sticky", "sticky_minutes": 10}, {"tier": 2, "session_mode": "rotating"}])
    s = r._session_settings()
    assert s["max_usage_count"] == 50 and 0 < s["max_age"].total_seconds() < 600
    r.tiers = TierPlan([{"tier": 1, "session_mode": "sticky", "sticky_minutes": 120}])
    assert r._session_settings()["max_age"].total_seconds() <= 30 * 60


def test_a_session_is_never_replayed_through_another_tier():
    from app.fetching.proxies import TierPlan
    from app.jobs.runner import Deferred, JobRunner

    r = JobRunner.__new__(JobRunner)
    r.tiers = TierPlan([])
    r._proxy_tier_by_url = {"http://t1": 1, "http://t2": 2}
    r._admitted = set()
    r.doc = {"protection": {"sessions_retired": 0}}
    retired = []
    session = SimpleNamespace(user_data={}, retire=lambda: retired.append(1))
    req = SimpleNamespace(url="http://shop.example/p/1", user_data={}, unique_key="k")
    r._check_session_tier(SimpleNamespace(session=session, proxy_info=SimpleNamespace(url="http://t1"), request=req))
    with pytest.raises(Deferred):
        r._check_session_tier(SimpleNamespace(session=session, proxy_info=SimpleNamespace(url="http://t2"), request=req))
    assert retired and r.doc["protection"]["sessions_retired"] == 1


# ---------------------------------------------------------------- delivery after downtime, body signature
def test_delivery_gap_never_bursts_after_downtime():
    from app.events.delivery import DeliveryWorker

    w = DeliveryWorker(SimpleNamespace(settings=settings(webhook_schedule_seconds=[0, 10, 30, 60, 120], webhook_repeat_seconds=3600, webhook_jitter=0.0)))
    gaps = [w._next_gap(n) for n in range(1, 8)]
    assert gaps[:4] == [10.0, 20.0, 30.0, 60.0] and gaps[4:] == [3600.0, 3600.0, 3600.0]
    assert all(g > 0 for g in gaps)  # each next attempt is planned from the previous one, never "due already"


def test_body_signature_vector():
    from app.events.journal import sign_body

    body = json.loads((Path(__file__).resolve().parents[2] / "contract" / "examples" / "webhook_event.request.json").read_text())
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    secret = "fs_whsec_3b1f0e6c9a2d47f8b5c1d0e9"
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    assert sign_body(secret, raw) == expected
    vector = json.loads((Path(__file__).resolve().parents[2] / "contract" / "examples" / "webhook_body_signature.vector.json").read_text())
    assert sign_body(vector["signing_secret"], vector["body"].encode()) == vector["x_scraper_body_signature"]


# ---------------------------------------------------------------- gunzip cap, sitemap parsing
def test_gunzip_is_capped():
    from app.fetching.oneoff import Fetched, gunzip_capped

    bomb = gzip.compress(b"\0" * (20 * 1024 * 1024))
    assert len(gunzip_capped(bomb, 1024 * 1024)) == 1024 * 1024
    assert len(Fetched("u", "u", 200, gzip.compress(b"User-agent: *\n")).text) == 14


def test_sitemap_parser_index_urlset_text_gzip():
    from app.jobs.discovery import parse_sitemap

    idx = b"<?xml version='1.0'?><sitemapindex xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'><sitemap><loc>https://a.example/s1.xml</loc></sitemap></sitemapindex>"
    assert parse_sitemap(idx, "https://a.example/sitemap.xml") == ([], ["https://a.example/s1.xml"])
    us = gzip.compress(b"<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'><url><loc>https://a.example/p/1</loc><lastmod>2026-11-01</lastmod></url></urlset>")
    pages, nested = parse_sitemap(us, "https://a.example/s1.xml")
    assert [p[0] for p in pages] == ["https://a.example/p/1"] and pages[0][1].year == 2026 and nested == []
    assert parse_sitemap(b"https://a.example/x\nnot a url\n", "https://a.example/s.txt")[0][0][0] == "https://a.example/x"


# ---------------------------------------------------------------- key derivation
def test_scrypt_keys_and_legacy_blobs_stay_readable():
    from app.core.db import Database
    from app.core.secrets import SecretStore, derive_key, legacy_key

    passphrase = "a-long-passphrase-0123456789"
    assert derive_key(passphrase) != hashlib.sha256(passphrase.encode()).digest()
    db = Database(Path(tempfile.mkdtemp()) / "t.sqlite3")
    old = SecretStore(db, None)
    old.key = legacy_key(passphrase)
    old.key_id = hashlib.sha256(old.key).hexdigest()[:16]
    old.put("run_1", "webhook", {"signing_secret": "x"})
    new = SecretStore(db, passphrase)
    assert new.readable("run_1", "webhook") and new.get("run_1", "webhook") == {"signing_secret": "x"}
    new.put("run_2", "webhook", {"signing_secret": "y"})
    assert new.undecryptable_runs() == 0


def test_protection_report_refuses_without_ownership_flag():
    import subprocess

    root = Path(__file__).resolve().parents[2]
    proc = subprocess.run([sys.executable, str(root / "scripts" / "protection_report.py"), "https://example.invalid/"],
                          capture_output=True, text=True, timeout=60, cwd=str(root))
    assert proc.returncode == 2 and "--i-own-this-site" in proc.stderr


def test_storm_proxies_is_refused_naming_generic():
    from app.core.schemas import schema_errors

    prof = {"profile_name": "p", "proxy_tiers": [{"tier": 1, "provider": "storm_proxies", "protocol": "http", "host": "gw.example", "port": 3128,
                                                  "auth_mode": "ip_allowlist", "session_mode": "rotating"}],
            "solver": None, "webhook": {"url": "https://h.example/x", "signing_secret": "fs_whsec_0123456789abcdef"}}
    errors = schema_errors("connection_profile.schema.json", prof, "profile")
    hit = [e for e in errors if e["field"] == "profile.proxy_tiers[0].provider"]
    assert hit and "generic" in hit[0]["message"] and "storm_proxies" in hit[0]["message"], errors



def test_presets_hold_the_launch_window():
    from app.fetching.browsers import WINDOW, consistent_preset

    for _ in range(10):
        preset = consistent_preset("windows")
        assert preset is not None
        scr = preset["screen"]
        assert scr.get("availWidth", scr["width"]) >= WINDOW[0] and scr.get("availHeight", scr["height"]) >= WINDOW[1]


def test_exit_geo_context_kwargs():
    from app.fetching.geo import ExitGeo

    g = ExitGeo(ip="5.6.7.8", timezone="Europe/Riga", locale="lv-LV", latitude=56.9, longitude=24.1, country="LV", source="echo")
    kw = g.context_kwargs(camoufox=True)
    assert kw["timezone_id"] == "Europe/Riga" and kw["locale"] == "lv-LV" and kw["webrtc_ip"] == "5.6.7.8"
    assert kw["geolocation"]["latitude"] == 56.9 and "permissions" not in kw
    assert g.context_kwargs(camoufox=False)["permissions"] == ["geolocation"] and "webrtc_ip" not in g.context_kwargs(camoufox=False)
