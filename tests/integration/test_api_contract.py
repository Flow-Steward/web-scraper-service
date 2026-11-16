"""Contract test: every endpoint's real responses validate against contract/schemas."""

from __future__ import annotations

import copy
import hashlib
import json

import httpx

from tests.conftest import (
    HOOK_SECRET, PART_MAPPING, PRODUCT_MAPPING, SHOP, SHOP_SITE, SOLVER, TOKEN, check, ensure_mappings, err, fast, merged_config,
    platform, profile, start,
)


def test_auth_and_conventions(service):
    raw = httpx.Client(base_url=service.client.base_url, timeout=10)
    err(raw.get("/health"), 401, "unauthorized")
    err(raw.get("/health", headers={"Authorization": "Bearer nope"}), 401, "unauthorized")
    err(service.get("/v1/nothing"), 404, "not_found")
    err(service.client.delete("/v1/runs"), 405, "method_not_allowed")
    err(service.client.post("/v1/config/validate", content=b"{}", headers={"Content-Type": "text/plain"}), 415, "unsupported_media_type")
    err(service.client.post("/v1/config/validate", content=b"{nope", headers={"Content-Type": "application/json"}), 400, "invalid_json")
    body = err(service.post("/v1/config/validate", {"profile": {}, "bogus": 1}), 422, "validation_failed")
    assert body["error"]["fields"][0] == {"field": "bogus", "code": "unknown_field", "message": body["error"]["fields"][0]["message"]}
    err(service.get("/v1/runs/run_DOESNOTEXIST"), 404, "run_not_found")
    err(service.post("/v1/events/evt_NOPE/claim", {"claimant": {"job_id": "j"}}), 404, "event_not_found")
    err(service.get("/v1/mapping-requests/mrq_NOPE"), 404, "mapping_request_not_found")
    err(service.get("/v1/runs", params={"cursor": "garbage"}), 400, "invalid_cursor")


def test_health_schema(service):
    body = check(service.get("/health"), "job_config.schema.json#/$defs/service_health", 200)
    assert body["service"]["api_versions"] == ["v1"]
    assert body["protection"]["challenge_click_allowed"] is False and body["protection"]["solvers_allowed"] is False
    assert "click" not in body["protection"]["steps_allowed"] and "solver" not in body["protection"]["steps_allowed"]
    assert {b["name"]: b["present"] for b in body["browsers"]} == {"camoufox": True, "chromium": True}


def test_validate_valid_invalid_and_warnings(service):
    prof = profile()
    prof["defaults"] = {"max_concurrency": 3, "delay_min_seconds": 2}
    body = check(service.post("/v1/config/validate", {"run_inputs": {"site_url": SHOP + "/", "max_pages": 50}, "site_settings": {"max_pages": 70, "fetch_mode": ""}, "profile": prof}),
                 "job_config.schema.json#/$defs/validate_response", 200)
    assert body["valid"] and body["config"]["max_pages"] == 50 and body["sources"]["max_pages"] == "run_inputs"
    assert body["config"]["max_concurrency"] == 3 and body["sources"]["max_concurrency"] == "profile_defaults"
    assert body["sources"]["fetch_mode"] == "default"
    check_root = check  # noqa
    body = check(service.post("/v1/config/validate", {"run_inputs": {"site_url": SHOP + "/", "max_pages": 250000, "challenge_click": True, "use_solver": True, "respect_robots": False}, "profile": prof}),
                 "job_config.schema.json#/$defs/validate_response", 200)
    assert body["valid"] is False and body["config"] is None
    assert body["errors"] == [{"field": "max_pages", "code": "limit_exceeded", "message": body["errors"][0]["message"], "limit": 100000}]
    assert {w["code"] for w in body["warnings"]} == {"challenge_click_not_allowed", "solver_not_allowed", "robots_override_not_allowed"}
    # a missing secret is a rule error (200), not a 422
    bad = profile(proxy_tiers=[{"tier": 1, "provider": "generic", "protocol": "http", "host": "127.0.0.1", "port": 8606, "auth_mode": "user_password", "session_mode": "rotating", "username": "u"}])
    body = check(service.post("/v1/config/validate", {"run_inputs": {"site_url": SHOP + "/"}, "profile": bad}), "job_config.schema.json#/$defs/validate_response", 200)
    assert body["errors"][0]["code"] == "secret_missing" and body["errors"][0]["field"] == "profile.proxy_tiers[0].password"
    # contract examples are accepted by shape
    from app.core.schemas import CONTRACT_DIR

    ex = json.loads((CONTRACT_DIR / "examples" / "post_config_validate.request.json").read_text())
    check(service.post("/v1/config/validate", ex), "job_config.schema.json#/$defs/validate_response", 200)


def test_idempotency(service, fixture_world):
    cfg = merged_config(service, {"site_url": SHOP + "/", "single_page": True, **fast()})
    err(service.client.post("/v1/runs", json={"config": cfg, "profile": profile(), "platform": platform("j")}), 400, "idempotency_key_required")
    err(service.client.post("/v1/runs", json={"config": cfg, "profile": profile(), "platform": platform("j")}, headers={"Idempotency-Key": "bad key!"}), 400, "idempotency_key_invalid")
    key = "fs:job_idem:start_run"
    first = start(service, cfg, key, job="job_idem")
    a = check(first, "run_summary.schema.json", 201)
    assert first.headers["location"] == f"/v1/runs/{a['run_id']}"
    other_profile = profile("rotated")  # profile is not part of the fingerprint
    again = service.client.post("/v1/runs", json={"config": cfg, "profile": other_profile, "platform": platform("job_idem")}, headers={"Idempotency-Key": key})
    b = check(again, "run_summary.schema.json", 200)
    assert b["run_id"] == a["run_id"]
    changed = dict(cfg, max_pages=7)
    body = err(service.client.post("/v1/runs", json={"config": changed, "profile": profile(), "platform": platform("job_idem")}, headers={"Idempotency-Key": key}), 409, "idempotency_key_reused")
    assert body["error"]["details"]["run_id"] == a["run_id"]
    listed = service.get("/v1/runs", params={"platform_job_id": "job_idem"}).json()
    assert [i["run_id"] for i in listed["items"]] == [a["run_id"]]
    run = service.wait_run(a["run_id"])
    assert run["state"] == "completed"
    # validation error creates nothing
    body = err(start(service, dict(cfg, max_pages=999999), "fs:job_idem:other"), 422, "validation_failed")
    assert body["error"]["fields"][0]["code"] == "limit_exceeded"


def test_runs_list_pagination(service, fixture_world):
    cfg = merged_config(service, {"site_url": SHOP + "/", "single_page": True, **fast()})
    ids = []
    for i in range(3):
        ids.append(start(service, cfg, f"page-test-{i}", job=f"job_page_{i}").json()["run_id"])
    page1 = check(service.get("/v1/runs", params={"limit": 2, "project_id": "prj_test"}), "run_summary.schema.json#/$defs/run_list", 200)
    assert len(page1["items"]) == 2 and page1["next_cursor"]
    page2 = check(service.get("/v1/runs", params={"limit": 2, "project_id": "prj_test", "cursor": page1["next_cursor"]}), "run_summary.schema.json#/$defs/run_list", 200)
    seen = [i["run_id"] for i in page1["items"] + page2["items"]]
    assert len(seen) == len(set(seen))
    stamps = [i["created_at"] for i in page1["items"] + page2["items"]]
    assert stamps == sorted(stamps, reverse=True)
    for rid in ids:
        service.wait_run(rid)


def test_mappings_versions_and_conflicts(service):
    site = "versions.example"
    m = copy.deepcopy(PART_MAPPING)
    r1 = check(service.put(f"/v1/sites/{site}/mappings", {"template_id": "spare_part", "expected_version": 0, "mapping": m}), "mapping.schema.json#/$defs/activate_response", 200)
    assert r1["changed"] and r1["active"]["version"] == 1
    sha = hashlib.sha256(json.dumps(m, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    assert r1["active"]["content_sha256"] == sha
    # same content: no-op even with a stale expected_version
    same = check(service.put(f"/v1/sites/{site}/mappings", {"template_id": "spare_part", "expected_version": 0, "mapping": m}), "mapping.schema.json#/$defs/activate_response", 200)
    assert same["changed"] is False and same["active"]["version"] == 1
    m2 = copy.deepcopy(m)
    m2["label"] = "v2"
    two = check(service.put(f"/v1/sites/{site}/mappings", {"template_id": "spare_part", "expected_version": 1, "mapping": m2}), "mapping.schema.json#/$defs/activate_response", 200)
    assert two["active"]["version"] == 2
    # two activations built against the same version: the second is refused
    m3 = copy.deepcopy(m)
    m3["label"] = "v3"
    body = err(service.put(f"/v1/sites/{site}/mappings", {"template_id": "spare_part", "expected_version": 1, "mapping": m3}), 409, "mapping_version_conflict")
    assert body["error"]["details"]["active_version"] == 2
    # re-activating old content gets a new number
    old = check(service.put(f"/v1/sites/{site}/mappings", {"template_id": "spare_part", "expected_version": 2, "mapping": m}), "mapping.schema.json#/$defs/activate_response", 200)
    assert old["active"]["version"] == 3
    allv = check(service.get(f"/v1/sites/{site}/mappings", params={"versions": "all"}), "mapping.schema.json#/$defs/site_mappings", 200)
    t = allv["templates"][0]
    assert t["active_version"] == 3 and t["versions_total"] == 3 and [v["version"] for v in t["versions"]] == [3, 2, 1]
    assert [v["status"] for v in t["versions"]] == ["active", "superseded", "superseded"]
    nocontent = service.get(f"/v1/sites/{site}/mappings", params={"include_content": "false"}).json()
    assert "mapping" not in nocontent["templates"][0]["versions"][0]
    assert service.get("/v1/sites/unknown.example/mappings").json() == {"site": "unknown.example", "templates": []}
    bad = copy.deepcopy(m)
    bad["fields"].append(copy.deepcopy(bad["fields"][0]))
    body = err(service.put(f"/v1/sites/{site}/mappings", {"template_id": "other", "expected_version": 0, "mapping": bad}), 422, "validation_failed")
    assert {f["code"] for f in body["error"]["fields"]} == {"template_id_mismatch", "duplicate_field_name"}
    sites = check(service.get("/v1/sites"), "mapping.schema.json#/$defs/site_list", 200)
    assert any(s["site"] == site for s in sites["items"])


def test_samples_mapping_test_and_tests(service, fixture_world):
    ensure_mappings(service, SHOP_SITE, PRODUCT_MAPPING, PART_MAPPING)
    cfg = merged_config(service, {"site_url": SHOP + "/", "sample_count": 3, "url_exclude": ["/js/"], **fast()})
    body = check(service.post("/v1/samples", {"config": cfg, "profile": profile(), "urls": [SHOP + "/produkt/leuchte-02", SHOP + "/ersatzteile/teil-03", SHOP + "/gone/x"]}),
                 "samples.schema.json", 200)
    assert len(body["samples"]) == 2 and body["warnings"]
    s0 = body["samples"][0]
    assert s0["template_hint"]["matched_template_id"] == "product" and s0["extracted"]["title"]
    assert "<script" not in s0["distilled_html"]
    discovered = check(service.post("/v1/samples", {"config": cfg, "profile": profile()}), "samples.schema.json", 200)
    assert len(discovered["samples"]) == 3
    ids = [s["sample_id"] for s in body["samples"]]
    test = check(service.post("/v1/mappings/test", {"site": SHOP_SITE, "mappings": [PRODUCT_MAPPING, PART_MAPPING], "sample_ids": ids, "required_fill_threshold": 0.9}),
                 "mapping_test.schema.json#/$defs/response", 200)
    assert test["passed"] and test["unmatched_sample_ids"] == []
    only_product = check(service.post("/v1/mappings/test", {"site": SHOP_SITE, "mappings": [PRODUCT_MAPPING], "sample_ids": ids, "required_fill_threshold": 0.9}),
                         "mapping_test.schema.json#/$defs/response", 200)
    assert only_product["passed"] is False and len(only_product["unmatched_sample_ids"]) == 1
    relaxed = service.post("/v1/mappings/test", {"site": SHOP_SITE, "mappings": [PRODUCT_MAPPING], "sample_ids": ids, "required_fill_threshold": 0.9, "require_all_samples_matched": False}).json()
    assert relaxed["passed"] is True
    strict = service.post("/v1/mappings/test", {"site": SHOP_SITE, "mappings": [PRODUCT_MAPPING], "sample_ids": ids[:1], "required_fill_threshold": 0.9, "required_fields": ["nope"]}).json()
    assert strict["passed"] is False
    body = err(service.post("/v1/mappings/test", {"site": SHOP_SITE, "mappings": [PRODUCT_MAPPING], "sample_ids": ["smp_UNKNOWN"], "required_fill_threshold": 0.9}), 404, "sample_not_found")
    assert body["error"]["details"]["sample_ids"] == ["smp_UNKNOWN"]
    analysis = check(service.post("/v1/analyze", {"config": cfg, "profile": profile()}), "analysis.schema.json", 200)
    assert analysis["sitemap"]["found"] and analysis["sitemap"]["url_count"] >= 30 and analysis["robots"]["present"]
    assert analysis["recommended"]["discovery_mode"] in ("sitemap", "sitemap_and_crawl")
    assert "Product" in analysis["structured_data"]["jsonld_types"] or analysis["structured_data"]["jsonld_types"] == []
    plain = merged_config(service, {"site_url": "http://plain.test:8602/"})
    a2 = check(service.post("/v1/analyze", {"config": plain, "profile": profile()}), "analysis.schema.json", 200)
    assert a2["sitemap"]["found"] is False and a2["recommended"]["discovery_mode"] == "crawl"


def test_proxy_and_solver_tests(service, fixture_world):
    tiers = [
        {"tier": 1, "provider": "generic", "protocol": "http", "host": "127.0.0.1", "port": 8606, "auth_mode": "user_password", "username": "tier1user", "password": "tier1pass", "session_mode": "rotating", "country": "DE"},
        {"tier": 2, "provider": "generic", "protocol": "http", "host": "127.0.0.1", "port": 8607, "auth_mode": "user_password", "username": "tier2user", "password": "WRONG", "session_mode": "sticky", "sticky_minutes": 5},
        {"tier": 3, "provider": "generic", "protocol": "http", "host": "127.0.0.1", "port": 8699, "auth_mode": "ip_allowlist", "session_mode": "rotating", "country": "FR"},
    ]
    prof = profile(proxy_tiers=tiers)
    ok = check(service.post("/v1/proxies/test", {"profile": prof, "tier": 1}), "connection_profile.schema.json#/$defs/proxy_test_response", 200)
    assert ok["ok"] and ok["exit_ip"] == "203.0.113.7" and ok["country"] == "DE"
    auth = check(service.post("/v1/proxies/test", {"profile": prof, "tier": 2}), "connection_profile.schema.json#/$defs/proxy_test_response", 200)
    assert auth["ok"] is False and auth["code"] == "proxy_auth_failed"
    conn = check(service.post("/v1/proxies/test", {"profile": prof, "tier": 3}), "connection_profile.schema.json#/$defs/proxy_test_response", 200)
    assert conn["ok"] is False and conn["code"] == "proxy_connect_failed"
    err(service.post("/v1/proxies/test", {"profile": profile(), "tier": 1}), 422, "validation_failed")
    solver = {"provider": "capsolver", "api_key": "fx-solver-key", "endpoint": SOLVER, "max_solves_per_job": 5}
    s_ok = check(service.post("/v1/solvers/test", {"profile": profile(solver=solver)}), "connection_profile.schema.json#/$defs/solver_test_response", 200)
    assert s_ok["ok"] and s_ok["balance"] == 12.5
    s_bad = check(service.post("/v1/solvers/test", {"profile": profile(solver=dict(solver, api_key="wrong-key"))}), "connection_profile.schema.json#/$defs/solver_test_response", 200)
    assert s_bad["ok"] is False and s_bad["code"] == "solver_auth_failed" and "wrong-key" not in s_bad.get("message", "")
    s_down = check(service.post("/v1/solvers/test", {"profile": profile(solver=dict(solver, endpoint="http://127.0.0.1:8698"))}), "connection_profile.schema.json#/$defs/solver_test_response", 200)
    assert s_down["code"] == "solver_unreachable"
    log = service.log()
    for secret in ("tier1pass", "fx-solver-key", "wrong-key", HOOK_SECRET):
        assert secret not in log


def test_result_download_and_unextracted(service, fixture_world):
    ensure_mappings(service, SHOP_SITE, PRODUCT_MAPPING, PART_MAPPING)
    urls = [{"url": f"{SHOP}/produkt/leuchte-0{i}"} for i in range(1, 5)] + [{"url": f"{SHOP}/gone/a"}, {"url": f"{SHOP}/private/x"}]
    cfg = merged_config(service, {"discovery_mode": "url_list", "urls": urls, "fetch_mode": "http", **fast()})
    r = start(service, cfg, "download-test")
    run_id = r.json()["run_id"]
    early = service.get(f"/v1/runs/{run_id}/result")
    if early.status_code == 409:
        assert err(early, 409, "result_not_ready")["error"]["details"]["state"] in ("queued", "running", "stopping")
    run = check(service.get(f"/v1/runs/{service.wait_run(run_id)['run_id']}"), "run_summary.schema.json", 200)
    desc = check(service.get(f"/v1/runs/{run_id}/result"), "result_descriptor.schema.json#/$defs/service_result", 200)
    full = service.get(desc["download_path"])
    assert full.status_code == 200 and full.headers["content-type"].startswith("application/x-ndjson")
    data = full.content
    assert len(data) == desc["size_bytes"] == int(full.headers["content-length"]) and hashlib.sha256(data).hexdigest() == desc["sha256"]
    assert full.headers["etag"] == f'"{desc["sha256"]}"' and full.headers["accept-ranges"] == "bytes"
    assert full.headers["content-disposition"] == f'attachment; filename="{desc["file_name"]}"'
    import base64

    assert full.headers["repr-digest"] == f"sha-256=:{base64.b64encode(bytes.fromhex(desc['sha256'])).decode()}:"
    lines = data.decode().splitlines()
    assert len(lines) == desc["record_count"] == 4 and data.endswith(b"\n")
    ids = [json.loads(l)["record_id"] for l in lines]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
    for line in lines:
        from app.core.schemas import validator

        assert not list(validator("record.schema.json").iter_errors(json.loads(line)))
    part = service.get(desc["download_path"], headers={"Range": "bytes=10-19"})
    assert part.status_code == 206 and part.content == data[10:20] and part.headers["content-range"] == f"bytes 10-19/{len(data)}"
    tail = service.get(desc["download_path"], headers={"Range": "bytes=-5"})
    assert tail.status_code == 206 and tail.content == data[-5:]
    resume = service.get(desc["download_path"], headers={"Range": "bytes=100-"})
    assert resume.content == data[100:]
    err(service.get(desc["download_path"], headers={"Range": f"bytes={len(data) + 5}-"}), 416, "range_not_satisfiable")
    many = service.get(desc["download_path"], headers={"Range": "bytes=0-1,5-6"})
    assert many.status_code == 200 and many.content == data
    assert service.get(desc["download_path"], headers={"If-None-Match": f'"{desc["sha256"]}"'}).status_code == 304
    stale_if_range = service.get(desc["download_path"], headers={"Range": "bytes=0-3", "If-Range": '"other"'})
    assert stale_if_range.status_code == 200 and stale_if_range.content == data
    unex = check(service.get(f"/v1/runs/{run_id}/unextracted"), "run_summary.schema.json#/$defs/unextracted_list", 200)
    reasons = {i["reason"] for i in unex["items"]}
    assert reasons == {"not_found", "robots_disallowed"} and unex["total"] == 2
    assert run["outcome"] == "partial"  # robots_disallowed makes it partial
    nd = service.get(f"/v1/runs/{run_id}/unextracted", headers={"Accept": "application/x-ndjson"})
    assert nd.headers["content-type"].startswith("application/x-ndjson") and len(nd.text.splitlines()) == 2
    err(service.get(f"/v1/runs/{run_id}/unextracted", headers={"Accept": "text/html"}), 406, "not_acceptable")
    filtered = service.get(f"/v1/runs/{run_id}/unextracted", params={"reason": "not_found", "limit": 1}).json()
    assert filtered["total"] == 1 and filtered["next_cursor"] is None


def test_stop_and_resume_shapes(service, fixture_world):
    cfg = merged_config(service, {"site_url": SHOP + "/slow/a", "single_page": True, **fast()})
    run_id = start(service, cfg, "stop-shape").json()["run_id"]
    stopped = check(service.post(f"/v1/runs/{run_id}/stop", {"note": "operator"}), "run_summary.schema.json", 200)
    assert stopped["state"] in ("completed", "stopping")
    again = check(service.post(f"/v1/runs/{run_id}/stop", {}), "run_summary.schema.json", 200)
    final = service.wait_run(run_id)
    assert final["stop_reason"] == "stopped" and final["outcome"] == "partial"
    resumed = check(service.post(f"/v1/runs/{run_id}/resume", {"profile": profile()}), "run_summary.schema.json", 200)
    assert resumed["state"] == "completed"
    err(service.get(f"/v1/runs/{run_id}/result/download", headers={"Range": "bytes=0-"}), 416, "range_not_satisfiable") if final["result"]["size_bytes"] == 0 else None
    for event in final["events"]:
        from app.core.schemas import validator

        assert not list(validator("event.schema.json#/$defs/event_record").iter_errors(event))
    assert again["run_id"] == run_id
