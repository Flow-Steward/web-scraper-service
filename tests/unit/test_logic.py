"""Pure logic: config merge and rules, proxy presets and tier logic, events state machine, delivery schedule."""

import asyncio
import json

import pytest

from app.core.config import apply_rules, dedupe_url_list, merge_layers
from app.core.db import Database
from app.core.errors import ApiError
from app.events.journal import EventJournal
from app.fetching.proxies import TierPlan, build_username, tier_url
from app.settings import LIMIT_DEFAULTS, Settings


def settings(**kw):
    s = Settings(api_token="t", data_dir="/tmp", secrets_key=None, limits={k: v[0] for k, v in LIMIT_DEFAULTS.items()})
    s.allow_private_targets = True
    for k, v in kw.items():
        setattr(s, k, v)
    return s


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    from app import settings as mod

    mod.set_settings(settings())
    yield
    mod.set_settings(None)


BROWSERS = {"camoufox": {"present": True}, "chromium": {"present": True}}


def test_merge_layers_unset_values_fall_through():
    merged, sources = merge_layers(
        {"site_url": "https://a.example/", "max_pages": None, "fields": [], "fetch_mode": ""},
        {"max_pages": 50, "challenge_click": False, "headers": {}},
        {"max_pages": 70, "max_concurrency": 8, "delay_min_seconds": 0},
    )
    assert merged["max_pages"] == 50 and sources["max_pages"] == "site_settings"
    assert merged["max_concurrency"] == 8 and sources["max_concurrency"] == "profile_defaults"
    assert merged["delay_min_seconds"] == 0 and sources["delay_min_seconds"] == "profile_defaults"
    assert merged["challenge_click"] is False and sources["challenge_click"] == "site_settings"
    assert merged["fetch_mode"] == "auto" and sources["fetch_mode"] == "default"
    assert merged["required_fields"] == ["title"]
    merged, _ = merge_layers({"site_url": "https://a.example/", "fields": ["name", "price"]}, {}, {})
    assert merged["required_fields"] == ["name"]


def run(coro):
    return asyncio.run(coro)


def test_rules_errors_and_warnings_all_at_once():
    merged, _ = merge_layers({"site_url": "https://a.example/", "max_pages": 999999, "challenge_click": True, "use_solver": True,
                              "respect_robots": False, "urls": [{"url": "https://a.example/x"}], "required_fields": ["zzz"],
                              "delay_min_seconds": 5, "delay_max_seconds": 1, "url_include": ["("], "headers": {"Cookie": "x"},
                              "use_proxy_tiers": [3]}, {}, {})
    fixed, errors, warnings = run(apply_rules(merged, {}, settings(), BROWSERS))
    codes = {e["code"] for e in errors}
    assert {"limit_exceeded", "required_field_not_in_fields", "delay_range_invalid", "pattern_invalid", "header_not_allowed", "proxy_tier_not_configured"} <= codes
    assert next(e for e in errors if e["code"] == "limit_exceeded")["limit"] == 100000
    wcodes = {w["code"] for w in warnings}
    assert {"challenge_click_not_allowed", "solver_not_allowed", "robots_override_not_allowed", "invalid_value"} <= wcodes
    assert fixed["challenge_click"] is False and fixed["use_solver"] is False and fixed["respect_robots"] is True and fixed["urls"] == []


def test_rotating_tier_refused_with_click_when_allowed():
    prof = {"proxy_tiers": [{"tier": 1, "provider": "generic", "protocol": "http", "host": "p", "port": 1, "auth_mode": "ip_allowlist", "session_mode": "rotating"}]}
    merged, _ = merge_layers({"site_url": "https://a.example/", "challenge_click": True}, {}, {})
    _, errors, _ = run(apply_rules(merged, prof, settings(challenge_click_allowed=True), BROWSERS))
    assert [e["code"] for e in errors] == ["rotating_proxy_with_challenge_handling"]
    _, errors, warnings = run(apply_rules(merged, prof, settings(challenge_click_allowed=False), BROWSERS))
    assert errors == [] and warnings[0]["code"] == "challenge_click_not_allowed"


def test_solver_and_browser_and_secret_rules():
    merged, _ = merge_layers({"site_url": "https://a.example/", "use_solver": True, "browser": "camoufox"}, {}, {})
    _, errors, _ = run(apply_rules(merged, {"proxy_tiers": [{"tier": 1, "provider": "oxylabs", "auth_mode": "user_password", "session_mode": "sticky", "protocol": "http", "host": "h", "port": 1}]},
                                   settings(solvers_allowed=True), {"camoufox": {"present": False}, "chromium": {"present": True}}))
    codes = sorted(e["code"] for e in errors)
    assert codes == ["browser_not_available", "secret_missing", "secret_missing", "solver_not_configured"]


def test_url_list_dedupe_and_required():
    urls = [{"url": "https://a.example/x?utm_source=1"}, {"url": "https://a.example/x"}, {"url": "https://a.example/x", "key": "K"}]
    assert len(dedupe_url_list(urls)) == 2
    merged, _ = merge_layers({"discovery_mode": "url_list"}, {}, {})
    _, errors, _ = run(apply_rules(merged, {}, settings(), BROWSERS))
    assert errors[0]["field"] == "urls" and errors[0]["code"] == "required"


@pytest.mark.parametrize("provider,expected", [
    ("oxylabs", "customer-acme-cc-DE-city-new_york-sessid-"),
    ("bright_data", "brd-customer-hl_1-zone-res-country-de-city-newyork-session-"),
    ("decodo", "user-acme-country-de-city-new_york-session-"),
    ("generic", "acme"),
])
def test_preset_usernames(provider, expected):
    user = "brd-customer-hl_1-zone-res" if provider == "bright_data" else "acme"
    tier = {"tier": 1, "provider": provider, "auth_mode": "user_password", "username": user, "password": "pw", "country": "DE",
            "city": "New York", "session_mode": "sticky" if provider != "generic" else "rotating", "sticky_minutes": 30}
    name = build_username(tier, "sess1")
    assert name.startswith(expected)
    if provider != "generic":
        assert name == build_username(tier, "sess1") != build_username(tier, "sess2")  # stable per session


def test_tier_url_and_plan():
    t1 = {"tier": 1, "provider": "generic", "protocol": "http", "host": "p1", "port": 8000, "auth_mode": "user_password", "username": "u@x", "password": "p:w", "session_mode": "rotating"}
    t2 = {"tier": 2, "provider": "custom_list", "auth_mode": "user_password", "session_mode": "sticky", "sticky_minutes": 5, "proxy_urls": ["http://a:b@h1:1", "http://a:b@h2:2"]}
    assert tier_url(t1, None) == "http://u%40x:p%3Aw@p1:8000"
    assert tier_url(t2, "s") == tier_url(t2, "s")
    plan = TierPlan([t2, t1])
    assert plan.tier_for("h")["tier"] == 1
    assert plan.escalate("h") and plan.tier_for("h")["tier"] == 2
    assert not plan.escalate("h") and plan.at_highest("h")
    assert plan.tier_for("other")["tier"] == 1
    restored = TierPlan([t1, t2], plan.host_level)
    assert restored.tier_for("h")["tier"] == 2


def journal(tmp_path, timeout=60):
    return EventJournal(Database(tmp_path / "db.sqlite"), timeout)


def test_event_state_machine(tmp_path):
    j = journal(tmp_path)
    eid = j.create("run_1", "run_completed", "run_1", {"outcome": "complete", "stop_reason": "finished"})
    body = j.payload(eid)
    assert body["delivery_generation"] == 1 and list(body)[:6] == ["event_id", "event_type", "run_id", "resource_id", "occurred_at", "delivery_generation"]
    a = j.claim(eid, {"job_id": "job_a"})
    assert a["state"] == "claimed" and a["progress"]["last_stage"] is None
    assert j.claim(eid, {"job_id": "job_a"})["claim_id"] == a["claim_id"]  # idempotent for the same job
    with pytest.raises(ApiError) as exc:
        j.claim(eid, {"job_id": "job_b"})
    assert exc.value.code == "event_already_claimed"
    with pytest.raises(ApiError) as exc:
        j.stage(eid, a["claim_id"], "completed")
    assert exc.value.code == "stage_out_of_order"
    rec = j.stage(eid, a["claim_id"], "processed")
    first = rec["progress"]["stages"][0]["recorded_at"]
    assert j.stage(eid, a["claim_id"], "processed")["progress"]["stages"][0]["recorded_at"] == first
    with pytest.raises(ApiError) as exc:
        j.resend(eid, "resume", False, None)
    assert exc.value.code == "event_claim_live"
    rec = j.resend(eid, "resume", True, 1)
    assert rec["state"] == "pending" and rec["delivery"]["generation"] == 2 and rec["progress"]["last_stage"] == "processed"
    with pytest.raises(ApiError) as exc:
        j.resend(eid, "resume", True, 1)
    assert exc.value.code == "event_generation_conflict"
    with pytest.raises(ApiError) as exc:
        j.complete(eid, a["claim_id"])
    assert exc.value.code == "claim_superseded"
    b = j.claim(eid, {"job_id": "job_b"})
    assert b["resend_mode"] == "resume" and b["progress"]["last_stage"] == "processed" and b["delivery_generation"] == 2
    done = j.complete(eid, b["claim_id"])
    assert done["state"] == "handled"
    assert j.complete(eid, b["claim_id"])["state"] == "handled"
    with pytest.raises(ApiError) as exc:
        j.claim(eid, {"job_id": "job_c"})
    assert exc.value.code == "event_already_handled"
    rec = j.resend(eid, "from_start", False, None)
    assert rec["progress"]["stages"] == [] and rec["delivery"]["resend_mode"] == "from_start"


def test_stale_claim(tmp_path, monkeypatch):
    import app.events.journal as mod
    from datetime import timedelta
    from app.core import timeutil

    j = journal(tmp_path, timeout=60)
    eid = j.create("run_1", "run_completed", "run_1", {"outcome": "partial", "stop_reason": "stopped"})
    a = j.claim(eid, {"job_id": "job_a"})
    later = timeutil.now() + timedelta(seconds=120)
    monkeypatch.setattr(mod, "now", lambda: later)
    with pytest.raises(ApiError) as exc:
        j.claim(eid, {"job_id": "job_b"})
    assert exc.value.code == "event_stale"
    row = j.db.one("SELECT * FROM events WHERE event_id=?", (eid,))
    assert j.record(row)["state"] == "stale"
    # the holder may continue
    assert j.stage(eid, a["claim_id"], "processed")["state"] == "claimed"


def test_problem_events_once(tmp_path):
    j = journal(tmp_path)
    assert j.create("run_1", "problem", "run_1", {"problem_code": "block_rate_high"}, problem_key="block_rate_high:")
    assert j.create("run_1", "problem", "run_1", {"problem_code": "block_rate_high"}, problem_key="block_rate_high:") is None


def test_delivery_schedule_offsets():
    from app.events.delivery import DeliveryWorker

    class S:
        settings = settings(webhook_jitter=0.0)

    w = DeliveryWorker(S())
    assert [w._next_offset(i) for i in range(12)] == [0, 10, 30, 60, 120, 300, 600, 1200, 1800, 3600, 7200, 10800]
    S.settings.webhook_jitter = 0.1
    for _ in range(50):
        assert 54 <= w._next_offset(3) <= 66


def test_secret_store_key_change(tmp_path):
    from app.core.secrets import SecretStore, SecretsUnavailable

    db = Database(tmp_path / "s.sqlite")
    a = SecretStore(db, "first-key-0123456789")
    a.put("run_1", "webhook", {"url": "u", "signing_secret": "s" * 20})
    raw = db.one("SELECT ciphertext FROM run_secrets")["ciphertext"]
    assert b"ssssssss" not in raw
    assert a.get("run_1", "webhook")["signing_secret"] == "s" * 20
    b = SecretStore(db, "second-key-0123456789")
    assert not b.readable("run_1", "webhook") and b.undecryptable_runs() == 1
    with pytest.raises(SecretsUnavailable):
        b.get("run_1", "webhook")


def test_fingerprint_ignores_number_form():
    from app.jobs.manager import fingerprint

    assert fingerprint({"a": 1.0, "b": [2.0]}, {"x": 1}) == fingerprint({"b": [2], "a": 1}, {"x": 1})
    assert fingerprint({"a": 1.5}, {}) != fingerprint({"a": 1}, {})


def test_challenge_detection():
    from app.fetching.challenge import detect_html

    assert detect_html("<title>Just a moment...</title><div>cf_chl_opt</div>", 403).kind == "challenge"
    assert detect_html("<h1>Access denied</h1><p>Error 1020</p>", 403).kind == "hard_block"
    assert detect_html("<p>We use cloudflare for speed</p>" + "x " * 2000, 200).kind == "none"  # no bare marker
    long_form = "<form><div class='cf-turnstile' data-sitekey='k'></div></form>" + "<p>text</p>" * 400
    assert detect_html(long_form, 200).kind == "none"
