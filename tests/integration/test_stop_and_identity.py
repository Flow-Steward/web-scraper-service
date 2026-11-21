"""Findings on a Cloudflare-protected site:

* within one analysis or samples call the protection route runs at most once per
  host; after it fails, samples fetch nothing more of the host and answer at once with
  `stopped` and a `protection_not_passed` warning; analysis does not run the route again
  for robots.txt or sitemaps of that host.
* one browser identity per exit: analysis and samples through the same exit show
  the same preset (UA, platform, screen, WebGL, cores); another exit gets another one.
* The chromium solver route regression (INJECT_TOKEN_JS once lost) is covered by
  test_protection.py::test_solver_passes_and_is_counted.
"""

from __future__ import annotations

import time

import pytest

from tests.conftest import GUARD, check, merged_config, profile

HOST = "guard.test:8603"
TIER1 = {"tier": 1, "provider": "generic", "protocol": "http", "host": "127.0.0.1", "port": 8606, "auth_mode": "user_password",
         "username": "tier1user", "password": "tier1pass", "session_mode": "sticky", "sticky_minutes": 10}
TIER2 = {"tier": 2, "provider": "generic", "protocol": "http", "host": "127.0.0.1", "port": 8607, "auth_mode": "user_password",
         "username": "tier2user", "password": "tier2pass", "session_mode": "sticky", "sticky_minutes": 10}


def _hits(fixture_world) -> dict:
    return fixture_world.state(8603)["hits"]


def test_samples_stop_after_the_first_failed_challenge(guarded_service, fixture_world):
    fixture_world.set(8603, guard_mode="noframe", guard_ttl=1000, reset_clearances=True, reset_hits=True)
    cfg = merged_config(guarded_service, {"site_url": GUARD + "/p/1", "browser": "chromium", "challenge_click": True})
    urls = [f"{GUARD}/p/{i}" for i in range(1, 6)]
    started = time.monotonic()
    body = check(guarded_service.post("/v1/samples", {"config": cfg, "profile": profile(), "urls": urls}), "samples.schema.json", 200)
    elapsed = time.monotonic() - started
    assert body["samples"] == []
    assert body["stopped"] == {"reason": "protection_not_passed", "host": HOST, "url": urls[0], "skipped_urls": 4}, body
    warn = [w for w in body["warnings"] if w["code"] == "protection_not_passed"]
    assert len(warn) == 1 and warn[0]["field"] == "urls[0]", body["warnings"]
    msg = warn[0]["message"]
    assert msg.startswith(f"{HOST} shows a Cloudflare interstitial challenge that was not passed (waited 3 s, the checkbox was not found, "
                          "no solver requested); no proxy. No further pages of this host were fetched (4 pages skipped)."), msg
    # one trace: only the URL actually tried; the skipped URLs were never requested
    assert [t["url"] for t in body["protection_trace"]] == [urls[0]]
    hits = _hits(fixture_world)
    assert all(hits.get(f"8603/p/{i}", 0) == 0 for i in range(2, 6)), hits
    assert elapsed < 45, elapsed  # one route (3 s wait in the tests), not five


def test_samples_without_a_failure_have_no_stop(guarded_service, fixture_world):
    fixture_world.set(8603, guard_mode="shadow", guard_ttl=1000, reset_clearances=True)
    cfg = merged_config(guarded_service, {"site_url": GUARD + "/p/1", "browser": "chromium", "challenge_click": True})
    body = check(guarded_service.post("/v1/samples", {"config": cfg, "profile": profile(), "urls": [f"{GUARD}/p/1", f"{GUARD}/p/2"]}),
                 "samples.schema.json", 200)
    assert len(body["samples"]) == 2 and "stopped" not in body
    assert not [w for w in body["warnings"] if w["code"] == "protection_not_passed"]


def test_analysis_does_not_rerun_the_route_for_a_sitemap_of_a_failed_host(guarded_service, fixture_world):
    """The route ran on the category page and again on the challenged sitemap."""
    fixture_world.set(8603, guard_mode="noframe", guard_ttl=1000, guard_discovery="sitemap", reset_clearances=True, reset_hits=True)
    try:
        cfg = merged_config(guarded_service, {"site_url": GUARD + "/p/1", "browser": "chromium"})
        body = check(guarded_service.post("/v1/analyze", {"config": cfg, "profile": profile()}), "analysis.schema.json", 200)
        hits = _hits(fixture_world)
    finally:
        fixture_world.set(8603, guard_discovery=False)
    assert [t["url"] for t in body["protection_trace"]] == [GUARD + "/p/1"], body["protection_trace"]
    assert body["protection"]["kind"] == "challenge" and body["protection"]["passed_at_step"] is None
    warn = [w for w in body["warnings"] if w["code"] == "protection_not_passed"]
    assert len(warn) == 1 and warn[0]["field"] == "site_url" and warn[0]["message"].startswith(HOST + " shows"), body["warnings"]
    assert "plain HTTP" in warn[0]["message"]
    # the sitemap was asked for once over plain HTTP (challenged): no browser request, no route
    assert hits.get("8603/robots.txt") == 1 and hits.get("8603/guarded-sitemap.xml") == 1, hits
    assert body["sitemap"]["found"] is None
    assert body["sitemap"]["inspection"] == "limited"
    assert body["sitemap"]["url_count"] is None


def test_analysis_with_robots_behind_the_failed_challenge_runs_one_route(guarded_service, fixture_world):
    fixture_world.set(8603, guard_mode="noframe", guard_ttl=1000, guard_discovery=True, reset_clearances=True, reset_hits=True)
    try:
        cfg = merged_config(guarded_service, {"site_url": GUARD + "/p/1", "browser": "chromium"})
        body = check(guarded_service.post("/v1/analyze", {"config": cfg, "profile": profile()}), "analysis.schema.json", 200)
        hits = _hits(fixture_world)
    finally:
        fixture_world.set(8603, guard_discovery=False)
    assert len(body["protection_trace"]) == 1 and body["protection_trace"][0]["url"].endswith("/robots.txt"), body["protection_trace"]
    assert hits.get("8603/p/1") == 1, hits  # the start page over HTTP only, never in the browser after the failure
    assert [w["code"] for w in body["warnings"]].count("protection_not_passed") == 1


@pytest.mark.parametrize("browser", ["chromium", "camoufox"])
def test_one_identity_per_exit(guarded_service, fixture_world, browser):
    fixture_world.set(8603, guard_mode="shadow", guard_ttl=1000, reset_clearances=True)
    prof = profile(proxy_tiers=[TIER1])
    cfg = merged_config(guarded_service, {"site_url": GUARD + "/p/1", "browser": browser, "challenge_click": True}, prof=prof)
    a = check(guarded_service.post("/v1/analyze", {"config": cfg, "profile": prof}), "analysis.schema.json", 200)
    fixture_world.set(8603, reset_clearances=True)
    s = check(guarded_service.post("/v1/samples", {"config": cfg, "profile": prof, "urls": [f"{GUARD}/p/2"]}), "samples.schema.json", 200)
    ta, ts = a["protection_trace"][0], s["protection_trace"][0]
    assert ta["exit_ip"] == ts["exit_ip"] == "203.0.113.7"
    pa, ps = ta["environment"]["preset_id"], ts["environment"]["preset_id"]
    assert pa and pa == ps, (pa, ps)
    va, vs = ta["environment"]["browser_view"], ts["environment"]["browser_view"]
    for key in ("user_agent", "platform", "screen", "hardware_concurrency", "webgl_vendor", "webgl_renderer"):
        assert va.get(key) == vs.get(key), (key, va.get(key), vs.get(key))
    assert va["timezone"] == "Europe/Berlin" and va["language"] == ta["environment"]["exit"]["locale"], va  # the exit's place still applies
    # another exit IP (the tier-2 proxy's echo answers another address): another identity
    fixture_world.set(8603, reset_clearances=True)
    fixture_world.set(8605, echo_ip_tier2="198.51.100.23")
    try:
        prof2 = profile(proxy_tiers=[TIER2])
        cfg2 = merged_config(guarded_service, {"site_url": GUARD + "/p/3", "browser": browser, "challenge_click": True}, prof=prof2)
        b = check(guarded_service.post("/v1/analyze", {"config": cfg2, "profile": prof2}), "analysis.schema.json", 200)
    finally:
        fixture_world.set(8605, echo_ip_tier2=None)
    tb = b["protection_trace"][0]
    assert tb["exit_ip"] == "198.51.100.23" and tb["environment"]["preset_id"] not in (None, pa), tb["environment"]
    assert "new browser identity" in guarded_service.log()


def test_one_skipped_page_is_singular(guarded_service, fixture_world):
    fixture_world.set(8603, guard_mode="noframe", guard_ttl=1000, reset_clearances=True)
    cfg = merged_config(guarded_service, {"site_url": GUARD + "/p/1", "browser": "chromium"})
    body = check(guarded_service.post("/v1/samples", {"config": cfg, "profile": profile(), "urls": [f"{GUARD}/p/1", f"{GUARD}/p/2"]}),
                 "samples.schema.json", 200)
    msg = next(w["message"] for w in body["warnings"] if w["code"] == "protection_not_passed")
    assert msg.endswith("No further pages of this host were fetched (1 page skipped)."), msg
    assert body["stopped"]["skipped_urls"] == 1


# ---------------------------------------------------------------- one load per URL, a visitor's tab, re-challenge
def _guard_loads(fixture_world, path=None) -> list[dict]:
    loads = fixture_world.state(8603)["guard_loads"]
    return [x for x in loads if path is None or x["path"] == path]


def test_the_start_page_is_loaded_once_for_discovery_and_as_a_sample(guarded_service, fixture_world):
    from tests.integration.test_click_route import GM
    from tests.conftest import ensure_mappings

    ensure_mappings(guarded_service, HOST, GM)
    fixture_world.set(8603, guard_mode="shadow", guard_ttl=1000, guard_discovery=False, reset_clearances=True)
    # url_include keeps only the start page itself as a candidate: discovery loads it, the sample takes it
    cfg = merged_config(guarded_service, {"site_url": GUARD + "/p/1", "browser": "chromium", "challenge_click": True,
                                          "url_include": ["/p/1$"], "sample_count": 1, "delay_min_seconds": 0, "delay_max_seconds": 0})
    body = check(guarded_service.post("/v1/samples", {"config": cfg, "profile": profile()}), "samples.schema.json", 200)
    assert [s["url"] for s in body["samples"]] == [GUARD + "/p/1"], body["warnings"]
    assert body["samples"][0]["fetch"]["fetch_mode"] == "browser" and body["samples"][0]["fetch"]["passed_at_step"] == "click"
    assert len(body["protection_trace"]) == 1 and len(fixture_world.state(8603)["clicks"]) == 1
    # one HTTP load, one browser load and the challenge's own reload after the click: never loaded again
    assert len(_guard_loads(fixture_world, "/p/1")) == 3, _guard_loads(fixture_world)


def test_after_a_pass_the_tab_goes_on_like_a_visitor(guarded_service, fixture_world):
    fixture_world.set(8603, guard_mode="shadow", guard_ttl=1000, reset_clearances=True)
    cfg = merged_config(guarded_service, {"site_url": GUARD + "/p/1", "browser": "chromium", "challenge_click": True,
                                          "delay_min_seconds": 1, "delay_max_seconds": 1})
    urls = [f"{GUARD}/p/{i}" for i in (1, 2, 3)]
    body = check(guarded_service.post("/v1/samples", {"config": cfg, "profile": profile(), "urls": urls}), "samples.schema.json", 200)
    assert len(body["samples"]) == 3 and len(fixture_world.state(8603)["clicks"]) == 1
    browser_loads = [x for x in _guard_loads(fixture_world) if x["referer"]]
    by_path = {x["path"]: x for x in browser_loads}
    assert by_path["/p/2"]["referer"] == urls[0] and by_path["/p/3"]["referer"] == urls[1], browser_loads
    assert by_path["/p/3"]["at"] - by_path["/p/2"]["at"] >= 0.9  # delay_min..delay_max between navigations


def test_a_host_challenged_again_after_a_pass_gets_the_route_once_more(guarded_service, fixture_world):
    # the clearance lasts one load: the next page is challenged again and passes with a second click
    fixture_world.set(8603, guard_mode="shadow", guard_ttl=1, reset_clearances=True)
    cfg = merged_config(guarded_service, {"site_url": GUARD + "/p/1", "browser": "chromium", "challenge_click": True,
                                          "delay_min_seconds": 0, "delay_max_seconds": 0})
    urls = [f"{GUARD}/p/1", f"{GUARD}/p/2"]
    body = check(guarded_service.post("/v1/samples", {"config": cfg, "profile": profile(), "urls": urls}), "samples.schema.json", 200)
    assert len(body["samples"]) == 2 and "stopped" not in body, body["warnings"]
    first, second = body["protection_trace"]
    assert "passed_earlier" not in first and second["passed_earlier"] == {"url": urls[0], "step": "click"}
    assert second["passed_at_step"] == "click"


def test_a_host_that_fails_after_a_pass_is_stopped_and_the_message_says_so(guarded_service, fixture_world):
    fixture_world.set(8603, guard_mode="shadow", guard_ttl=1, guard_clear_limit=1, reset_clearances=True)
    try:
        cfg = merged_config(guarded_service, {"site_url": GUARD + "/p/1", "browser": "chromium", "challenge_click": True,
                                              "delay_min_seconds": 0, "delay_max_seconds": 0})
        urls = [f"{GUARD}/p/{i}" for i in (1, 2, 3)]
        body = check(guarded_service.post("/v1/samples", {"config": cfg, "profile": profile(), "urls": urls}), "samples.schema.json", 200)
    finally:
        fixture_world.set(8603, guard_clear_limit=None)
    assert [s["url"] for s in body["samples"]] == [urls[0]]
    assert body["stopped"] == {"reason": "protection_not_passed", "host": HOST, "url": urls[1], "skipped_urls": 1}
    second = body["protection_trace"][1]
    assert second["passed_earlier"] == {"url": urls[0], "step": "click"} and second["passed_at_step"] is None
    assert [s["step"] for s in second["steps"]][:2] == ["wait", "click"]  # the route ran once more
    msg = next(w["message"] for w in body["warnings"] if w["code"] == "protection_not_passed")
    assert "the host had passed a challenge earlier in this call (click) and was challenged again" in msg, msg
    assert msg.endswith("(1 page skipped)."), msg


# ---------------------------------------------------------------- Camoufox OS and canvas
def test_camoufox_variants_are_traced_with_the_canvas_self_check(guarded_service, fixture_world):
    fixture_world.set(8603, guard_mode="shadow", guard_ttl=1000, reset_clearances=True)
    envs = {}
    for os_, noise in (("windows", "auto"), ("linux", "auto"), ("windows", "off")):
        fixture_world.set(8603, reset_clearances=True)
        cfg = merged_config(guarded_service, {"site_url": GUARD + "/p/1", "browser": "camoufox", "challenge_click": True,
                                              "browser_os": os_, "canvas_noise": noise, "delay_min_seconds": 0, "delay_max_seconds": 0})
        assert cfg["browser_os"] == os_ and cfg["canvas_noise"] == noise
        try:
            resp = guarded_service.client.post("/v1/analyze", json={"config": cfg, "profile": profile()}, timeout=150)
        except Exception as exc:
            print(guarded_service.log()[-6000:])
            raise AssertionError(f"{os_}/{noise}: {type(exc).__name__}") from exc
        body = check(resp, "analysis.schema.json", 200)
        envs[(os_, noise)] = body["protection_trace"][0]["environment"]
    w, lin, off = envs[("windows", "auto")], envs[("linux", "auto")], envs[("windows", "off")]
    assert (w["browser_os"], w["canvas_noise"], w["canvas_readback"]) == ("windows", "auto", "noise"), w
    assert (lin["browser_os"], lin["canvas_readback"], lin["browser_view"]["platform"]) == ("linux", "exact", "Linux x86_64"), lin
    assert (off["canvas_readback"], off["webgl_readback"], off["browser_view"]["platform"]) == ("exact", "exact", "Win32"), off
    assert len({w["preset_id"], lin["preset_id"], off["preset_id"]}) == 3  # variants never share an identity
    defaults = merged_config(guarded_service, {"site_url": GUARD + "/p/1"})
    assert defaults["browser_os"] == "host" and defaults["canvas_noise"] == "auto"


def test_protection_report_runs_the_variant_matrix(guarded_service, fixture_world):
    import subprocess
    import sys
    import tempfile

    from tests.conftest import ROOT, base_env

    fixture_world.set(8603, guard_mode="shadow", guard_ttl=1000, reset_clearances=True)
    data = tempfile.mkdtemp(prefix="wss-report-")
    env = base_env(data)
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "protection_report.py"), GUARD + "/p/1", "--i-own-this-site",
                           "--browser", "camoufox,chromium", "--os", "linux", "--canvas-noise", "auto", "--wait", "3", "--click-wait", "8",
                           "--pause", "10", "--shots", data + "/shots"], env=env, cwd=str(ROOT), capture_output=True, text=True, timeout=240)
    assert proc.returncode == 0, proc.stderr[-3000:]
    rows = [line for line in proc.stdout.splitlines() if line.startswith("| camoufox") or line.startswith("| chromium")]
    assert len(rows) == 2, proc.stdout
    cam = [c.strip() for c in rows[0].split("|")]
    assert cam[2:9] == ["camoufox", "linux", "auto", "exact", "exact", "passed", "click"], cam
    chrome = [c.strip() for c in rows[1].split("|")]
    assert chrome[2] == "chromium" and chrome[5] == "exact" and chrome[7] == "passed", chrome
    assert cam[15].startswith("fp_") and chrome[15].startswith("fp_")
