"""Refusing fixture proxy exercises the real HTTP and browser transport paths.

Run in the canonical container with its installed Camoufox/Chromium binaries.
The host diagnostics use synthetic browser pipeline errors instead.
"""

import copy
import json
import sqlite3

import pytest

from tests.conftest import PRODUCT_MAPPING, SHOP, SHOP_SITE, check, ensure_mappings, fast, merged_config, profile, start
from tests.integration.test_fetching import TIERS


@pytest.mark.parametrize("mode,browser", [
    ("http", "chromium"), ("auto", "chromium"),
    ("browser", "chromium"), ("browser", "camoufox"),
])
def test_refused_proxy_pauses_and_corrected_profile_resumes(service, fixture_world, mode, browser):
    ensure_mappings(service, SHOP_SITE, PRODUCT_MAPPING)
    good = profile(proxy_tiers=copy.deepcopy(TIERS))
    refused = copy.deepcopy(good)
    refused["proxy_tiers"][0]["password"] = "fixture-refused-password"
    cfg = merged_config(service, {
        "discovery_mode": "url_list", "urls": [{"url": f"{SHOP}/produkt/leuchte-15"}],
        "fetch_mode": mode, "browser": browser, "max_retries": 0,
        **fast(max_concurrency=1),
    }, prof=refused)
    response = start(service, cfg, f"proxy-auth-{mode}-{browser}", prof=refused)
    assert response.status_code == 201, response.text
    run_id = response.json()["run_id"]
    paused = service.wait_run(run_id, states=("waiting_for_credentials", "completed", "failed"), timeout=60)
    check(service.get(f"/v1/runs/{run_id}"), "run_summary.schema.json", 200)
    if paused["state"] != "waiting_for_credentials":
        print(json.dumps({"run": paused, "unextracted": service.get(f"/v1/runs/{run_id}/unextracted").json()}, indent=2))
        print(service.log()[-20000:])
    assert paused["state"] == "waiting_for_credentials", paused
    assert paused["secrets"]["proxy_and_solver"] == "kept"
    assert paused["counters"]["records_extracted"] == 0
    assert paused["counters"]["retries"] == 0
    assert paused["counters"]["current_proxy_tier"] == 1
    assert paused["counters"]["proxy_requests_by_tier"].get("2", 0) == 0
    assert paused["protection"]["hard_blocks"] == 0
    assert paused["protection"]["challenges_met"] == 0
    assert paused["stop_reason"] is None
    assert len([e for e in paused["events"] if e["event_type"] == "problem"]) == 1
    with sqlite3.connect(f"{service.data_dir}/service.sqlite3") as db:
        payload = json.loads(db.execute("SELECT doc FROM events WHERE run_id=? AND event_type='problem'", (run_id,)).fetchone()[0])["payload"]
    assert payload["problem_code"] == "proxy_auth_failed"
    assert "Resume" in payload["message"]
    assert "fixture-refused-password" not in json.dumps(paused)
    assert "fixture-refused-password" not in service.log()

    resumed = service.post(f"/v1/runs/{run_id}/resume", {"profile": good})
    assert resumed.status_code == 200, resumed.text
    final = service.wait_run(run_id)
    assert final["state"] == "completed", final
    assert final["counters"]["records_extracted"] == 1
    assert final["counters"]["current_proxy_tier"] == 1
    assert final["counters"]["proxy_requests_by_tier"].get("2", 0) == 0
    assert final["protection"]["clicks_used"] == 0
    assert final["protection"]["solves_used"] == 0
