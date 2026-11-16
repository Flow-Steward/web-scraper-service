"""Result + events, signing, delivery across a restart, claims/resend, secrets, kill-and-restart."""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest

from tests.conftest import (
    HOOK, HOOK_SECRET, PART_MAPPING, PRODUCT_MAPPING, SECRETS_KEY, check, ensure_mappings, err, fast, merged_config, new_service,
    platform, profile, start,
)

SITE = "restart.test:8601"
BASE = "http://restart.test:8601"
TIER1 = {"tier": 1, "provider": "generic", "protocol": "http", "host": "127.0.0.1", "port": 8606, "auth_mode": "user_password",
         "username": "tier1user", "password": "tier1pass", "session_mode": "sticky", "sticky_minutes": 10}


@pytest.fixture(scope="module")
def svc(fixture_world):
    s = new_service(8092, WSS_CLAIM_TIMEOUT_SECONDS="60").start()
    yield s
    s.terminate()


def _deliveries_for(fixture_world, event_id):
    return [d for d in fixture_world.deliveries() if json.loads(d["body"]).get("event_id") == event_id]


def _wait(pred, timeout=90, what=""):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = pred()
        if value:
            return value
        time.sleep(0.3)
    raise AssertionError(f"timeout waiting for {what}")


def test_delivery_across_restart_and_handled_once(svc, fixture_world):
    ensure_mappings(svc, SITE, PRODUCT_MAPPING, PART_MAPPING)
    fixture_world.set(8604, hook_on=False, hook_secret=HOOK_SECRET, reset_deliveries=True)
    cfg = merged_config(svc, {"discovery_mode": "url_list", "urls": [{"url": f"{BASE}/produkt/leuchte-01"}], "fetch_mode": "http", **fast()})
    run_id = start(svc, cfg, "ev-restart", prof=profile("restart", proxy_tiers=[TIER1])).json()["run_id"]
    run = svc.wait_run(run_id)
    assert run["state"] == "completed"
    event = next(e for e in run["events"] if e["event_type"] == "run_completed")
    _wait(lambda: len(_deliveries_for(fixture_world, event["event_id"])) >= 2, what="failed attempts")
    # after the final state: proxy credentials gone from the volume, signing secret kept
    import sqlite3

    db = sqlite3.connect(f"{svc.data_dir}/service.sqlite3")
    kinds = {r[0] for r in db.execute("SELECT kind FROM run_secrets WHERE run_id=?", (run_id,))}
    db.close()
    assert kinds == {"webhook"}
    assert run["secrets"] == {"proxy_and_solver": "discarded", "signing_secret": "kept"}
    svc.kill()
    svc.start()
    fixture_world.set(8604, hook_on=True)
    accepted = _wait(lambda: [d for d in _deliveries_for(fixture_world, event["event_id"]) if d["accepted"]], what="accepted delivery")
    attempts = _deliveries_for(fixture_world, event["event_id"])
    assert len(accepted) == 1 and accepted[0]["signature_ok"] and accepted[0]["timestamp_ok"] and accepted[0]["body_signature_ok"]
    # one generation: identical body and idempotency key on every attempt; signatures differ only by timestamp
    assert len({a["body"] for a in attempts}) == 1
    assert {a["headers"]["idempotency-key"] for a in attempts} == {f"{event['event_id']}.1"}
    assert all(a["headers"]["user-agent"].startswith("web-scraper-service/") for a in attempts)
    rec = svc.get(f"/v1/runs/{run_id}").json()
    state = next(e for e in rec["events"] if e["event_id"] == event["event_id"])
    assert state["state"] == "delivered" and state["delivery"]["attempts"] >= 3
    claim = check(svc.post(f"/v1/events/{event['event_id']}/claim", {"claimant": {"job_id": "job_handler_1", "workflow_id": "wf", "step_id": "claim"}}),
                  "event.schema.json#/$defs/claim_response", 200)
    assert claim["event"]["outcome"] == "complete" and claim["progress"]["last_stage"] is None
    err(svc.post(f"/v1/events/{event['event_id']}/claim", {"claimant": {"job_id": "job_handler_2"}}), 409, "event_already_claimed")
    check(svc.post(f"/v1/events/{event['event_id']}/stage", {"claim_id": claim["claim_id"], "stage": "processed"}), "event.schema.json#/$defs/event_record", 200)
    check(svc.post(f"/v1/events/{event['event_id']}/stage", {"claim_id": claim["claim_id"], "stage": "completed"}), "event.schema.json#/$defs/event_record", 200)
    done = check(svc.post(f"/v1/events/{event['event_id']}/complete", {"claim_id": claim["claim_id"]}), "event.schema.json#/$defs/event_record", 200)
    assert done["state"] == "handled"
    err(svc.post(f"/v1/events/{event['event_id']}/claim", {"claimant": {"job_id": "job_handler_3"}}), 409, "event_already_handled")
    # rotated signing secret: a resend with the connection's current secret is accepted
    fixture_world.set(8604, hook_secret="fs_whsec_rotated_000000000000002")
    resent = check(svc.post(f"/v1/events/{event['event_id']}/resend", {"mode": "resume", "webhook": {"url": f"{HOOK}/hook/restart", "signing_secret": "fs_whsec_rotated_000000000000002"}, "expected_generation": 1}),
                   "event.schema.json#/$defs/event_record", 200)
    assert resent["delivery"]["generation"] == 2 and resent["state"] == "pending"
    err(svc.post(f"/v1/events/{event['event_id']}/resend", {"mode": "resume", "webhook": {"url": f"{HOOK}/hook/restart", "signing_secret": "fs_whsec_rotated_000000000000002"}, "expected_generation": 1}), 409, "event_generation_conflict")
    second = _wait(lambda: [d for d in _deliveries_for(fixture_world, event["event_id"]) if d["accepted"] and d["headers"]["idempotency-key"].endswith(".2")], what="generation 2")
    assert json.loads(second[0]["body"])["delivery_generation"] == 2
    claim2 = svc.post(f"/v1/events/{event['event_id']}/claim", {"claimant": {"job_id": "job_handler_4"}}).json()
    assert claim2["resend_mode"] == "resume" and claim2["progress"]["last_stage"] == "completed"  # processing steps skipped
    err(svc.post(f"/v1/events/{event['event_id']}/complete", {"claim_id": claim["claim_id"]}), 409, "claim_superseded")
    fixture_world.set(8604, hook_secret=HOOK_SECRET)
    log = svc.log()
    for secret in ("tier1pass", HOOK_SECRET, "fs_whsec_rotated_000000000000002", SECRETS_KEY):
        assert secret not in log


def test_stale_claim_resend_from_start(svc, fixture_world):
    cfg = merged_config(svc, {"discovery_mode": "url_list", "urls": [{"url": f"{BASE}/produkt/leuchte-02"}], "fetch_mode": "http", **fast()})
    run = svc.wait_run(start(svc, cfg, "ev-stale").json()["run_id"])
    eid = next(e for e in run["events"] if e["event_type"] == "run_completed")["event_id"]
    a = svc.post(f"/v1/events/{eid}/claim", {"claimant": {"job_id": "job_killed"}}).json()
    svc.post(f"/v1/events/{eid}/stage", {"claim_id": a["claim_id"], "stage": "processed"})
    time.sleep(62)  # claim timeout 60 s (the contract's minimum)
    rec = svc.get(f"/v1/runs/{run['run_id']}").json()
    assert next(e for e in rec["events"] if e["event_id"] == eid)["state"] == "stale"
    err(svc.post(f"/v1/events/{eid}/claim", {"claimant": {"job_id": "job_new"}}), 409, "event_stale")
    body = err(svc.post(f"/v1/events/{eid}/resend", {"mode": "from_start", "webhook": {"url": f"{HOOK}/hook/x", "signing_secret": HOOK_SECRET}}), 409, "event_claim_live") if False else None
    resent = svc.post(f"/v1/events/{eid}/resend", {"mode": "from_start", "webhook": {"url": f"{HOOK}/hook/x", "signing_secret": HOOK_SECRET}}).json()
    assert resent["progress"]["stages"] == [] and resent["delivery"]["generation"] == 2
    b = svc.post(f"/v1/events/{eid}/claim", {"claimant": {"job_id": "job_new"}}).json()
    assert b["progress"]["last_stage"] is None and b["resend_mode"] == "from_start"
    err(svc.post(f"/v1/events/{eid}/complete", {"claim_id": a["claim_id"]}), 409, "claim_superseded")  # the killed run's late complete


def _slow_sitemap_run(svc, key, tiers=None):
    ensure_mappings(svc, SITE, PRODUCT_MAPPING, PART_MAPPING)
    cfg = merged_config(svc, {"site_url": BASE + "/", "discovery_mode": "sitemap", "fetch_mode": "http", "url_exclude": ["/js/", "/gone/", "/private/"],
                              "delay_min_seconds": 0.4, "delay_max_seconds": 0.5, "max_concurrency": 1}, prof=profile("restart", proxy_tiers=tiers or []))
    return start(svc, cfg, key, prof=profile("restart", proxy_tiers=tiers or [])).json()["run_id"]


def test_kill_and_restart_resumes_without_refetching(svc, fixture_world):
    fixture_world.set(8601, reset_hits=True, layout=1, layout_switch_after=None)
    run_id = _slow_sitemap_run(svc, "restart-resume", tiers=[TIER1])
    _wait(lambda: svc.get(f"/v1/runs/{run_id}").json()["counters"]["pages_fetched"] >= 8, what="8 pages")
    before = svc.get(f"/v1/runs/{run_id}").json()
    svc.kill()
    svc.start()
    run = svc.wait_run(run_id)
    assert run["state"] == "completed" and run["counters"]["records_extracted"] == 32
    hits = {k: v for k, v in fixture_world.state()["hits"].items() if "/produkt/" in k or "/ersatzteile/" in k}
    assert len(hits) == 32
    assert sum(v - 1 for v in hits.values()) <= 1  # at most the page in progress at the kill is fetched again
    assert run["counters"]["pages_fetched"] >= before["counters"]["pages_fetched"]
    assert run["counters"]["proxy_requests_by_tier"]["1"] >= 32 and run["counters"]["current_proxy_tier"] == 1
    lines = svc.get(run["result"]["download_path"]).text.splitlines()
    assert len(lines) == len({json.loads(l)["record_id"] for l in lines}) == 32


def test_sigterm_keeps_the_job(svc, fixture_world):
    run_id = _slow_sitemap_run(svc, "restart-sigterm")
    _wait(lambda: svc.get(f"/v1/runs/{run_id}").json()["counters"]["pages_fetched"] >= 3, what="3 pages")
    code = svc.terminate()
    # uvicorn re-raises SIGTERM after its graceful shutdown, so -15 is the normal exit code
    assert code in (0, -15) and "service stopping" in svc.log()
    svc.start()
    run = svc.wait_run(run_id)
    assert run["state"] == "completed" and run["counters"]["records_extracted"] == 32


def test_changed_key_waits_for_credentials_until_resume(svc, fixture_world):
    fixture_world.set(8604, hook_on=True, hook_secret=HOOK_SECRET)
    run_id = _slow_sitemap_run(svc, "restart-key", tiers=[TIER1])
    _wait(lambda: svc.get(f"/v1/runs/{run_id}").json()["counters"]["pages_fetched"] >= 3, what="3 pages")
    svc.kill()
    svc.start(WSS_SECRETS_KEY="another-secrets-key-0000000000")
    run = check(svc.get(f"/v1/runs/{run_id}"), "run_summary.schema.json", 200)
    assert run["state"] == "waiting_for_credentials"
    assert run["secrets"] == {"proxy_and_solver": "missing", "signing_secret": "missing"}
    problem = next(e for e in run["events"] if e["event_type"] == "problem")
    assert problem["state"] == "pending"
    time.sleep(1.5)
    problem = next(e for e in svc.get(f"/v1/runs/{run_id}").json()["events"] if e["event_type"] == "problem")
    assert problem["delivery"]["last_error"] == "signing_secret_unavailable"
    health = svc.get("/health").json()
    assert health["jobs"]["waiting_for_credentials"] >= 1 and health["secrets_key"]["undecryptable_jobs"] >= 1
    fetched = run["counters"]["pages_fetched"]
    time.sleep(2)
    assert svc.get(f"/v1/runs/{run_id}").json()["counters"]["pages_fetched"] == fetched  # nothing is fetched
    resumed = check(svc.post(f"/v1/runs/{run_id}/resume", {"profile": profile("restart", proxy_tiers=[TIER1])}), "run_summary.schema.json", 200)
    assert resumed["state"] == "running"
    run = svc.wait_run(run_id)
    assert run["state"] == "completed" and run["counters"]["records_extracted"] == 32
    _wait(lambda: all(e["state"] != "pending" for e in svc.get(f"/v1/runs/{run_id}").json()["events"]), what="events delivered")
    svc.kill()
    svc.start(WSS_SECRETS_KEY=SECRETS_KEY)


def test_crash_loop_fails_the_job(svc, fixture_world):
    run_id = _slow_sitemap_run(svc, "restart-crashloop")
    for _ in range(3):
        _wait(lambda: svc.get(f"/v1/runs/{run_id}").json()["state"] in ("running", "failed"), what="running")
        time.sleep(1.0)
        svc.kill()
        svc.start()
    run = svc.wait_run(run_id, timeout=30)
    assert run["state"] == "failed" and run["error"]["code"] == "crash_loop" and run["outcome"] is None
    assert any(e["event_type"] == "run_failed" for e in run["events"])
    err(svc.get(f"/v1/runs/{run_id}/result"), 409, "result_not_available")


def test_secrets_key_unavailable(fixture_world):
    s = new_service(8093, WSS_SECRETS_KEY="").start()
    try:
        cfg = merged_config(s, {"site_url": BASE + "/", "single_page": True})
        err(start(s, cfg, "nokey"), 503, "secrets_key_unavailable")
        health = s.get("/health").json()
        assert health["status"] == "degraded" and health["secrets_key"]["configured"] is False
    finally:
        s.terminate()


def test_result_size_limit_and_retention(fixture_world):
    import sqlite3

    s = new_service(8094, WSS_LIMIT_MAX_RESULT_BYTES="2500", WSS_RETENTION_INTERVAL_SECONDS="1").start()
    try:
        ensure_mappings(s, SITE, PRODUCT_MAPPING, PART_MAPPING)
        cfg = merged_config(s, {"site_url": BASE + "/", "discovery_mode": "sitemap", "fetch_mode": "http", "url_include": ["/produkt/"], **fast(max_concurrency=1)})
        run = s.wait_run(start(s, cfg, "size-limit").json()["run_id"])
        assert run["stop_reason"] == "max_result_size" and run["outcome"] == "partial"
        desc = s.get(f"/v1/runs/{run['run_id']}/result").json()
        assert 0 < desc["size_bytes"] <= 2500
        assert any(i["reason"] == "stopped" for i in s.get(f"/v1/runs/{run['run_id']}/unextracted").json()["items"])
        # retention: an expired result is 410, an expired job record is gone (404)
        db = sqlite3.connect(f"{s.data_dir}/service.sqlite3")
        doc = json.loads(db.execute("SELECT doc FROM runs WHERE run_id=?", (run["run_id"],)).fetchone()[0])
        doc["result"]["expires_at"] = "2020-01-01T00:00:00Z"
        db.execute("UPDATE runs SET doc=? WHERE run_id=?", (json.dumps(doc), run["run_id"]))
        db.commit()
        err(s.get(f"/v1/runs/{run['run_id']}/result"), 410, "result_expired")
        db.execute("UPDATE runs SET finished_at='2020-01-01T00:00:00Z' WHERE run_id=?", (run["run_id"],))
        db.commit()
        db.close()
        _wait(lambda: s.get(f"/v1/runs/{run['run_id']}").status_code == 404, timeout=30, what="retention")
        err(s.get(f"/v1/runs/{run['run_id']}"), 404, "run_not_found")
    finally:
        s.terminate()
