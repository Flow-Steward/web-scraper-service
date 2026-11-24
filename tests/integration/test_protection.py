"""Protection on the fixture: detection, wait, click, solver, session reuse, limits, flags, no secrets."""

from __future__ import annotations

import copy
import json

from tests.conftest import (
    GUARD, HOOK_SECRET, PRODUCT_MAPPING, SOLVER, check, ensure_mappings, err, fast, merged_config, profile, start,
)

GUARD_SITE = "guard.test:8603"
GM = copy.deepcopy(PRODUCT_MAPPING)
GM["match"] = {"url_patterns": ["/p/\\d+$"]}
SOLVER_CFG = {"provider": "capsolver", "api_key": "fx-solver-key", "endpoint": SOLVER, "max_solves_per_job": 3}
STICKY = {"tier": 1, "provider": "generic", "protocol": "http", "host": "127.0.0.1", "port": 8606, "auth_mode": "user_password",
          "username": "tier1user", "password": "tier1pass", "session_mode": "rotating"}


def _urls(n, start_at=1):
    return [{"url": f"{GUARD}/p/{i}"} for i in range(start_at, start_at + n)]


def _run(svc, key, mode, ttl=1000, prof=None, **inputs):
    from tests.conftest import Admin

    Admin().set(8603, guard_mode=mode, guard_ttl=ttl, reset_clearances=True)
    ensure_mappings(svc, GUARD_SITE, GM)
    prof = prof or profile()
    cfg = merged_config(svc, {"discovery_mode": "url_list", **fast(max_concurrency=1), **inputs}, prof=prof)
    r = start(svc, cfg, key, prof=prof)
    assert r.status_code == 201, r.text
    return svc.wait_run(r.json()["run_id"])


def test_flags_off_click_and_solver_are_never_used(service, fixture_world):
    health = service.get("/health").json()
    assert "click" not in health["protection"]["steps_allowed"] and "solver" not in health["protection"]["steps_allowed"]
    body = service.post("/v1/config/validate", {"run_inputs": {"site_url": GUARD + "/p/1", "challenge_click": True, "use_solver": True},
                                                "profile": profile(solver=SOLVER_CFG)}).json()
    assert body["valid"] and body["config"]["challenge_click"] is False and body["config"]["use_solver"] is False
    before = fixture_world.state(8605)["solver_calls"]
    # even a config that asks for both (sent directly to /v1/runs) is corrected, never used
    run = _run(service, "prot-flags-off", "click", urls=_urls(2), prof=profile(solver=SOLVER_CFG), browser="camoufox")
    assert run["config"]["challenge_click"] is False and run["config"]["use_solver"] is False
    p = run["protection"]
    assert p["clicks_used"] == 0 and p["solves_used"] == 0 and p["failed"] >= 1 and p["challenges_met"] >= 1
    assert fixture_world.state(8605)["solver_calls"] == before
    assert run["counters"]["pages_by_class"].get("blocked", 0) >= 1


def test_rotating_proxy_with_click_is_refused(guarded_service):
    prof = profile(proxy_tiers=[STICKY])
    body = check(guarded_service.post("/v1/config/validate", {"run_inputs": {"site_url": GUARD + "/p/1", "challenge_click": True}, "profile": prof}),
                 "job_config.schema.json#/$defs/validate_response", 200)
    assert body["valid"] is False and body["errors"][0]["code"] == "rotating_proxy_with_challenge_handling" and body["errors"][0]["field"] == "challenge_click"
    cfg = guarded_service.post("/v1/config/validate", {"run_inputs": {"site_url": GUARD + "/p/1"}, "profile": prof}).json()["config"]
    cfg["challenge_click"] = True
    resp = start(guarded_service, cfg, "prot-rotating", prof=prof)
    assert err(resp, 422, "validation_failed")["error"]["fields"][0]["code"] == "rotating_proxy_with_challenge_handling"


def test_click_passes_and_the_session_is_reused(guarded_service, fixture_world):
    for browser in ("camoufox", "chromium"):
        run = _run(guarded_service, f"prot-click-{browser}", "click", urls=_urls(5), challenge_click=True, browser=browser)
        p = run["protection"]
        assert run["counters"]["records_extracted"] == 5, (browser, run["counters"], p)
        assert p["challenges_met"] == 1 and p["passed_by_step"] == {"click": 1} and p["clicks_used"] == 1  # following pages: no new challenge
        assert run["counters"]["browser_fetches"] == 5  # the host goes to the browser after the challenge


def test_wait_step(guarded_service, fixture_world):
    run = _run(guarded_service, "prot-wait", "auto", urls=_urls(3), browser="camoufox")
    p = run["protection"]
    missed = guarded_service.get(f"/v1/runs/{run['run_id']}/unextracted").json()["items"]
    assert run["counters"]["records_extracted"] == 3 and p["clicks_used"] == 0, (p, missed, run["errors"][:5])
    # Under load the native auto-clear reload can finish before detection.
    # No route budget is consumed when the browser handler never saw it; an
    # earlier HTTP challenge is then credited to the successful browser load.
    assert (p["challenges_met"], p["passed_by_step"]) in [(0, {}), (1, {"wait": 1}), (1, {"camoufox": 1})], p
    assert p["failed"] == 0 and p["solves_used"] == 0


def test_challenge_returning_later_is_handled_again(guarded_service, fixture_world):
    run = _run(guarded_service, "prot-returning", "click", ttl=3, urls=_urls(6), challenge_click=True, browser="camoufox")
    p = run["protection"]
    assert run["counters"]["records_extracted"] == 6, (run["counters"], p)
    assert p["challenges_met"] >= 2 and p["passed_by_step"].get("click", 0) >= 2 and p["failed"] == 0


def test_solver_passes_and_is_counted(guarded_service, fixture_world):
    prof = profile(solver=SOLVER_CFG)
    run = _run(guarded_service, "prot-solver", "solver", urls=_urls(3), use_solver=True, browser="chromium", prof=prof)
    p = run["protection"]
    assert run["counters"]["records_extracted"] == 3, (run["counters"], p, run["errors"])
    assert p["passed_by_step"] == {"solver": 1} and p["solves_used"] == 1 and p["solves_limit"] == 3
    assert fixture_world.state(8605)["solver_calls"].get("createTask", 0) >= 1
    text = json.dumps(run) + guarded_service.log()
    assert "fx-solver-key" not in text and HOOK_SECRET not in text and "tier1pass" not in text


def test_limits_stop_clicking_and_report_a_problem(guarded_service, fixture_world):
    run = _run(guarded_service, "prot-limit", "click", ttl=1, urls=_urls(4), challenge_click=True, max_clicks_per_job=1, browser="camoufox")
    p = run["protection"]
    assert p["clicks_used"] == 1 and p["clicks_limit"] == 1 and p["failed"] >= 1
    problems = [e for e in run["events"] if e["event_type"] == "problem"]
    codes = {json.loads(json.dumps(e)).get("event_id") for e in problems}
    assert problems and len(codes) == len(problems)
    claim = guarded_service.post(f"/v1/events/{problems[0]['event_id']}/claim", {"claimant": {"job_id": "j"}}).json()
    assert claim["event"]["problem_code"] == "challenge_limit_reached"
    assert run["outcome"] == "partial"


def test_frame_not_found_is_counted(guarded_service, fixture_world):
    run = _run(guarded_service, "prot-noframe", "noframe", urls=_urls(1), challenge_click=True, browser="chromium")
    p = run["protection"]
    assert p["frame_not_found"] == 1 and p["clicks_used"] == 0 and p["failed"] == 1


def test_block_rule_ends_the_job(service, fixture_world):
    run = _run(service, "prot-blocked", "click", urls=_urls(8), fetch_mode="http")
    assert run["stop_reason"] == "blocked" and run["outcome"] == "partial", run["counters"]
    assert any(e["event_type"] == "problem" for e in run["events"])
    assert run["counters"]["pages_fetched"] < 8
