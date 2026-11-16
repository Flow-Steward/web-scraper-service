"""Live findings on a managed challenge: the Turnstile frame sits in a
closed shadow root, verification takes seconds after ONE click on the checkbox (a second
click restarts it), and analysis / samples must run the same route as a job."""

from __future__ import annotations

import copy

import pytest

from tests.conftest import GUARD, PRODUCT_MAPPING, check, ensure_mappings, fast, merged_config, profile, start

GM = copy.deepcopy(PRODUCT_MAPPING)
GM["match"] = {"url_patterns": ["/p/\\d+$"]}


def _shadow(fixture_world):
    fixture_world.set(8603, guard_mode="shadow", guard_ttl=1000, reset_clearances=True)


def _clicks(fixture_world) -> list[dict]:
    return fixture_world.state(8603)["clicks"]


@pytest.mark.parametrize("browser", ["chromium", "camoufox"])
def test_analyze_passes_a_shadow_root_challenge_with_one_click(guarded_service, fixture_world, browser):
    _shadow(fixture_world)
    cfg = merged_config(guarded_service, {"site_url": GUARD + "/p/1", "challenge_click": True, "browser": browser})
    body = check(guarded_service.post("/v1/analyze", {"config": cfg, "profile": profile()}), "analysis.schema.json", 200)
    assert body["protection"]["kind"] == "challenge" and body["protection"]["passed_at_step"] == "click", body["protection"]
    clicks = _clicks(fixture_world)
    assert len(clicks) == 1 and 8 <= clicks[0]["x"] <= 48 and not clicks[0]["busy"], clicks  # one click, on the checkbox


def test_samples_pass_the_challenge_and_share_the_clearance(guarded_service, fixture_world):
    _shadow(fixture_world)
    ensure_mappings(guarded_service, "guard.test:8603", GM)
    cfg = merged_config(guarded_service, {"site_url": GUARD + "/p/1", "challenge_click": True, "browser": "chromium", "fetch_mode": "auto"})
    body = check(guarded_service.post("/v1/samples", {"config": cfg, "profile": profile(), "urls": [f"{GUARD}/p/2", f"{GUARD}/p/3"]}),
                 "samples.schema.json", 200)
    assert len(body["samples"]) == 2, body["warnings"]
    assert [s["fetch"]["passed_at_step"] for s in body["samples"]] == ["click", "click"]
    assert len(_clicks(fixture_world)) == 1  # one browser session for the request: the second page reuses the clearance


def test_without_the_click_requested_analysis_reports_the_challenge(guarded_service, fixture_world):
    _shadow(fixture_world)
    cfg = merged_config(guarded_service, {"site_url": GUARD + "/p/1", "browser": "chromium"})
    body = guarded_service.post("/v1/analyze", {"config": cfg, "profile": profile()}).json()
    assert body["protection"]["kind"] == "challenge" and body["protection"]["passed_at_step"] is None
    assert body["recommended"]["needs_challenge_handling"] is True and _clicks(fixture_world) == []


def test_job_passes_with_one_click_and_reuses_the_clearance(guarded_service, fixture_world):
    _shadow(fixture_world)
    ensure_mappings(guarded_service, "guard.test:8603", GM)
    cfg = merged_config(guarded_service, {"discovery_mode": "url_list", "urls": [{"url": f"{GUARD}/p/{i}"} for i in range(1, 5)],
                                          "challenge_click": True, "browser": "chromium", **fast(max_concurrency=1)})
    run = guarded_service.wait_run(start(guarded_service, cfg, "shadow-job").json()["run_id"])
    p = run["protection"]
    assert run["counters"]["records_extracted"] == 4, (run["counters"], p)
    assert p["passed_by_step"] == {"click": 1} and p["clicks_used"] == 1 and p["frame_not_found"] == 0
    assert len(_clicks(fixture_world)) == 1


TIER1 = {"tier": 1, "provider": "generic", "protocol": "http", "host": "127.0.0.1", "port": 8606, "auth_mode": "user_password",
         "username": "tier1user", "password": "tier1pass", "session_mode": "sticky", "sticky_minutes": 10}


def _trace(body, url_part="/p/1"):
    traces = [t for t in body.get("protection_trace") or [] if url_part in t["url"]]
    assert traces, body.get("protection_trace")
    return traces[0]


def test_trace_of_a_passed_click_with_screenshot_and_log(guarded_service, fixture_world):
    _shadow(fixture_world)
    cfg = merged_config(guarded_service, {"site_url": GUARD + "/p/1", "challenge_click": True, "browser": "chromium", "debug": True})
    body = check(guarded_service.post("/v1/analyze", {"config": cfg, "profile": profile()}), "analysis.schema.json", 200)
    t = _trace(body)
    assert t["proxy_tier"] is None and t["passed_at_step"] == "click" and "fx_clearance" in t["cookies"]
    assert [s["step"] for s in t["steps"]] == ["wait", "click"] and t["steps"][0]["outcome"] == "timed_out"
    click = t["steps"][1]
    assert click["outcome"] == "passed" and click["frame_found_via"] == "frames" and click["x"] > 0 and click["seconds"] >= 3
    shot = guarded_service.get(f"/v1/screenshots/{t['screenshot_id']}")
    assert shot.status_code == 200 and shot.headers["content-type"] == "image/png" and shot.content[:4] == b"\x89PNG"
    assert guarded_service.get("/v1/screenshots/shot_NOPE").json()["error"]["code"] == "screenshot_not_found"
    assert "challenge http://guard.test:8603/p/1 tier=None" in guarded_service.log()


def test_trace_of_a_timed_out_wait_through_the_proxy_tier(guarded_service, fixture_world):
    _shadow(fixture_world)
    prof = profile(proxy_tiers=[TIER1])
    cfg = merged_config(guarded_service, {"site_url": GUARD + "/p/1", "browser": "chromium"}, prof=prof)
    body = check(guarded_service.post("/v1/analyze", {"config": cfg, "profile": prof}), "analysis.schema.json", 200)
    t = _trace(body)
    assert t["proxy_tier"] == 1 and t["passed_at_step"] is None and "screenshot_id" not in t
    assert [(s["step"], s["outcome"], s.get("reason")) for s in t["steps"]] == [
        ("wait", "timed_out", None), ("click", "skipped", "not_requested"), ("solver", "skipped", "not_requested")]
    assert "tier1pass" not in str(body) and "tier1pass" not in guarded_service.log()


def test_trace_of_a_missing_frame(guarded_service, fixture_world):
    fixture_world.set(8603, guard_mode="noframe", guard_ttl=1000, reset_clearances=True)
    cfg = merged_config(guarded_service, {"site_url": GUARD + "/p/1", "challenge_click": True, "browser": "chromium"})
    body = check(guarded_service.post("/v1/samples", {"config": cfg, "profile": profile(), "urls": [f"{GUARD}/p/1"]}), "samples.schema.json", 200)
    t = _trace(body)
    click = next(s for s in t["steps"] if s["step"] == "click")
    assert click["outcome"] == "frame_not_found" and click["frame_found_via"] == "none" and t["passed_at_step"] is None


def test_job_keeps_traces_of_its_first_challenges(guarded_service, fixture_world):
    _shadow(fixture_world)
    ensure_mappings(guarded_service, "guard.test:8603", GM)
    cfg = merged_config(guarded_service, {"discovery_mode": "url_list", "urls": [{"url": f"{GUARD}/p/{i}"} for i in (7, 8)],
                                          "challenge_click": True, "browser": "chromium", **fast(max_concurrency=1)})
    run = check(guarded_service.get(f"/v1/runs/{guarded_service.wait_run(start(guarded_service, cfg, 'shadow-trace-job').json()['run_id'])['run_id']}"),
                "run_summary.schema.json", 200)
    traces = run["protection"]["traces"]
    assert len(traces) == 1 and traces[0]["passed_at_step"] == "click" and traces[0]["steps"][-1]["frame_found_via"] == "frames"



@pytest.mark.parametrize("browser", ["chromium", "camoufox"])
def test_environment_follows_the_exit_ip_and_is_traced(guarded_service, fixture_world, browser):
    """Timezone, locale (Accept-Language) and geolocation come from the exit IP's place (the
    fixture echo answers Berlin), the browser's own view agrees, and the trace records both."""
    _shadow(fixture_world)
    prof = profile(proxy_tiers=[TIER1])
    cfg = merged_config(guarded_service, {"site_url": GUARD + "/p/1", "browser": browser, "challenge_click": True}, prof=prof)
    body = check(guarded_service.post("/v1/analyze", {"config": cfg, "profile": prof}), "analysis.schema.json", 200)
    t = _trace(body)
    exit_, view = t["environment"]["exit"], t["environment"]["browser_view"]
    assert exit_["ip"] == "203.0.113.7" and exit_["timezone"] == "Europe/Berlin" and (exit_["locale"] or "").startswith("de"), exit_
    assert t["exit_ip"] == "203.0.113.7" and t["proxy_tier"] == 1
    assert view["timezone"] == "Europe/Berlin" and view["language"] == exit_["locale"], view
    w, h = view["screen"]
    assert w >= view["window"][0] and h >= view["window"][1], view  # the window fits the claimed screen
    # the identity claims the host's OS (default browser_os host; Chromium likewise): Linux in the image
    assert view["platform"].startswith("Linux"), view
