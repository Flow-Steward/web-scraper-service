"""GET /v1/admin/storage and POST /v1/admin/cleanup over HTTP on a service of its own, with a
real run on the fixture shop: the result is kept until the platform has downloaded it."""

from __future__ import annotations

import httpx
import pytest

from tests.conftest import PRODUCT_MAPPING, SHOP, SHOP_SITE, check, ensure_mappings, err, fast, merged_config, new_service, start


@pytest.fixture(scope="module")
def admin_service(fixture_world):
    svc = new_service(8099).start()
    yield svc
    svc.terminate()


def test_storage_and_cleanup_with_a_real_run(admin_service):
    svc = admin_service
    ensure_mappings(svc, SHOP_SITE, PRODUCT_MAPPING)
    cfg = merged_config(svc, {"site_url": SHOP + "/", "discovery_mode": "sitemap", "fetch_mode": "http", "max_records": 3, **fast()})
    run_id = start(svc, cfg, "admin-run-1").json()["run_id"]
    run = svc.wait_run(run_id)
    assert run["state"] == "completed", run

    st = check(svc.get("/v1/admin/storage"), "storage.schema.json", 200)
    assert st["runs"]["by_state"]["completed"] == 1 and st["runs"]["active"] == 0
    assert st["kinds"]["result_files"]["items"] == 2 and st["kinds"]["result_files"]["unfetched_items"] == 2  # JSON Lines + CSV
    assert st["kinds"]["run_working_files"]["items"] == 1 and st["kinds"]["database"]["main_bytes"] > 0
    assert st["kinds"]["other"]["items"] >= 1  # the test harness writes service.log into the data directory
    assert st["last_cleanup"]["automatic"] is not None  # the sweeper ran at start-up

    body = {"result_files": {"older_than_days": 0}, "run_working_files": {"older_than_days": 0}}
    dry = check(svc.post("/v1/admin/cleanup", body), "cleanup.schema.json", 200)
    assert dry["dry_run"] is True and dry["removed"]["result_files"]["items"] == 0 and dry["kept"]["unfetched_results"] == 1
    assert dry["removed"]["run_working_files"]["items"] == 1
    assert svc.get(f"/v1/runs/{run_id}/result").status_code == 200

    # the platform downloads the result: from now on it may go
    dl = svc.get(f"/v1/runs/{run_id}/result/download")
    assert dl.status_code == 200 and dl.content
    st = check(svc.get("/v1/admin/storage"), "storage.schema.json", 200)
    assert st["kinds"]["result_files"]["unfetched_items"] == 0

    dry = check(svc.post("/v1/admin/cleanup", body), "cleanup.schema.json", 200)
    size = dry["removed"]["result_files"]["bytes"]
    csv_size = svc.get(f"/v1/runs/{run_id}/result").json()["csv"]["size_bytes"]
    assert dry["removed"]["result_files"]["items"] == 2 and size == len(dl.content) + csv_size
    assert svc.get(f"/v1/runs/{run_id}/result/download").status_code == 200  # a dry run removed nothing

    done = check(svc.post("/v1/admin/cleanup", dict(body, dry_run=False, compact_database=True)), "cleanup.schema.json", 200)
    assert done["dry_run"] is False and done["removed"]["result_files"] == {"items": 2, "bytes": size}
    assert done["compaction"]["checkpoint"] in ("done", "busy") and done["compaction"]["vacuum"] == "done"
    err(svc.get(f"/v1/runs/{run_id}/result"), 410, "result_expired")
    run = check(svc.get(f"/v1/runs/{run_id}"), "run_summary.schema.json", 200)
    assert run["state"] == "completed"  # the run record stays

    st = check(svc.get("/v1/admin/storage"), "storage.schema.json", 200)
    assert st["kinds"]["result_files"]["items"] == 0 and st["kinds"]["run_working_files"]["items"] == 0
    assert st["last_cleanup"]["manual"]["removed"]["result_files"]["items"] == 2

    done = check(svc.post("/v1/admin/cleanup", {"dry_run": False, "run_records": {"older_than_days": 0}}), "cleanup.schema.json", 200)
    assert done["removed"]["run_records"]["items"] == 1
    err(svc.get(f"/v1/runs/{run_id}"), 404, "run_not_found")


def test_a_ranged_download_that_ends_the_file_counts_as_fetched(admin_service):
    svc = admin_service
    ensure_mappings(svc, SHOP_SITE, PRODUCT_MAPPING)
    cfg = merged_config(svc, {"site_url": SHOP + "/", "discovery_mode": "sitemap", "fetch_mode": "http", "max_records": 2, **fast()})
    run_id = start(svc, cfg, "admin-run-2").json()["run_id"]
    svc.wait_run(run_id)
    body = {"result_files": {"older_than_days": 0}}
    size = svc.get(f"/v1/runs/{run_id}/result").json()["size_bytes"]
    assert svc.get(f"/v1/runs/{run_id}/result/download", headers={"Range": "bytes=0-9"}).status_code == 206
    assert svc.post("/v1/admin/cleanup", body).json()["kept"]["unfetched_results"] == 1  # the first bytes only
    assert svc.get(f"/v1/runs/{run_id}/result/download", headers={"Range": f"bytes=10-{size - 1}"}).status_code == 206
    out = svc.post("/v1/admin/cleanup", body).json()
    assert out["kept"]["unfetched_results"] == 0 and out["removed"]["result_files"]["items"] == 2


def test_admin_routes_need_the_token_and_a_valid_body(admin_service):
    svc = admin_service
    base = f"http://127.0.0.1:{svc.port}"
    assert httpx.get(base + "/v1/admin/storage").status_code == 401
    assert httpx.post(base + "/v1/admin/cleanup", json={}).status_code == 401
    body = err(svc.post("/v1/admin/cleanup", {"run_records": {"older_than_days": -1}}), 422, "validation_failed")
    assert body["error"]["fields"][0]["field"].startswith("run_records")
    err(svc.post("/v1/admin/cleanup", {"identities": True}), 422, "validation_failed")
    # an empty body is a dry run that removes nothing
    r = svc.client.post("/v1/admin/cleanup")
    out = check(r, "cleanup.schema.json", 200)
    assert out["dry_run"] is True and out["removed"] == {} and out["compaction"] is None
