"""GET /v1/admin/storage and POST /v1/admin/cleanup (README 16) on a service built in the
test (not started: no scheduler, no runners), with runs, files and samples written by hand."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from datetime import timedelta
from pathlib import Path

import pytest

from app.core.db import dumps
from app.core.schemas import schema_errors, validator
from app.core.timeutil import iso, now
from app.settings import LIMIT_DEFAULTS, Settings

DAY = 86400


def _validate(ref: str, body: dict) -> None:
    errors = list(validator(ref).iter_errors(body))
    assert not errors, [f"{list(e.absolute_path)}: {e.message}" for e in errors][:5]


@pytest.fixture
def svc():
    from app.service import Service

    data = Path(tempfile.mkdtemp(prefix="wss-admin-"))
    s = Settings(api_token="t", data_dir=data, secrets_key=None, limits={k: v[0] for k, v in LIMIT_DEFAULTS.items()})
    service = Service(s)
    yield service
    service.db.close()


def _write(path: Path, size: int, age_seconds: float = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    if age_seconds:
        t = time.time() - age_seconds
        os.utime(path, (t, t))


def add_run(svc, run_id: str, state: str, finished_days_ago: float | None = None, *, result_bytes: int = 0, job_bytes: int = 0, expires_days: float = 14) -> dict:
    site = "shop.test"
    ts = now()
    finished = iso(ts - timedelta(days=finished_days_ago)) if finished_days_ago is not None else None
    doc: dict = {"run_id": run_id, "state": state, "site": site}
    name = f"{site}_{run_id}.jsonl"
    data = Path(svc.settings.data_dir)
    if result_bytes:
        _write(data / "results" / name, result_bytes)
        doc["result"] = {"file_name": name, "expires_at": iso(ts + timedelta(days=expires_days))}
    if job_bytes:
        _write(data / "jobs" / run_id / "crawlee" / "key_value_stores" / "default" / "page.html", job_bytes)
    svc.db.execute(
        "INSERT INTO runs(run_id, idempotency_key, fingerprint, state, site, created_at, updated_at, finished_at, doc, runtime) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (run_id, "k-" + run_id, "f", state, site, iso(ts), iso(ts), finished, dumps(doc), "{}"),
    )
    svc.db.execute("INSERT INTO records(run_id, record_id, template_id, mapping_version, extracted_at, line) VALUES (?,?,?,?,?,?)", (run_id, "r1", "product", 1, iso(ts), "{}"))
    svc.db.execute("INSERT INTO urls(run_id, unique_key, url, source, status) VALUES (?,?,?,?,?)", (run_id, "u1", "http://shop.test/", "start", "done"))
    return doc


def mark_fetched(svc, run_id: str) -> None:
    svc.db.execute("INSERT INTO file_fetches(run_id, kind, fetched_at) VALUES (?, 'result', ?)", (run_id, iso(now())))


def add_sample(svc, sample_id: str, size: int, age_hours: float = 0) -> None:
    data = Path(svc.settings.data_dir)
    _write(data / "samples" / f"{sample_id}.html", size, age_hours * 3600)
    svc.db.execute("INSERT INTO samples(sample_id, site, expires_at, final_url, doc) VALUES (?,?,?,?,?)",
                   (sample_id, "shop.test", iso(now() + timedelta(hours=24 - age_hours)), "http://shop.test/p", "{}"))


def world(svc) -> None:
    """Two finished runs (one fetched, one not), one failed, one of each active state."""
    add_run(svc, "run_done1", "completed", 10, result_bytes=1000, job_bytes=500)
    mark_fetched(svc, "run_done1")
    add_run(svc, "run_done2", "completed", 10, result_bytes=2000, job_bytes=700)  # not fetched
    add_run(svc, "run_failed1", "failed", 10, job_bytes=300)
    for i, state in enumerate(("queued", "running", "waiting_for_credentials", "stopping")):
        add_run(svc, f"run_active{i}", state, None, job_bytes=100 + i)
    # a result being written by a running run
    _write(Path(svc.settings.data_dir) / "results" / "shop.test_run_active1.tmp", 50)
    add_sample(svc, "smp_a", 400, age_hours=5)
    _write(Path(svc.settings.data_dir) / "samples" / "smp_a.extra.json", 60, 5 * 3600)
    _write(Path(svc.settings.data_dir) / "samples" / "shots" / "shot_one.png", 900, 2 * 3600)
    _write(Path(svc.settings.data_dir) / "identities.json", 77)
    _write(Path(svc.settings.data_dir) / "lost+found" / "x", 11)


def everything(dry: bool, **extra) -> dict:
    body = {
        "dry_run": dry,
        "run_working_files": {"older_than_days": 0},
        "result_files": {"older_than_days": 0},
        "run_records": {"older_than_days": 0},
        "samples": {"older_than_hours": 0},
        "protection_screenshots": {"older_than_hours": 0},
        "compact_database": True,
    }
    body.update(extra)
    assert not schema_errors("cleanup.schema.json#/$defs/cleanup_request", body)
    return body


def files(svc) -> set[str]:
    data = Path(svc.settings.data_dir)
    return {str(p.relative_to(data)) for p in data.rglob("*") if p.is_file()}


async def test_storage_counts_every_kind(svc):
    from app.jobs.housekeeping import storage

    world(svc)
    body = await storage(svc)
    _validate("storage.schema.json", body)
    k = body["kinds"]
    assert k["run_working_files"]["items"] == 7 and k["run_working_files"]["active_items"] == 4
    assert k["run_working_files"]["bytes"] == 500 + 700 + 300 + 100 + 101 + 102 + 103
    assert k["result_files"]["items"] == 3 and k["result_files"]["bytes"] == 3050
    assert k["result_files"]["unfetched_items"] == 1 and k["result_files"]["unfetched_bytes"] == 2000
    assert k["result_files"]["active_items"] == 1 and k["result_files"]["orphan_items"] == 0
    assert k["samples"] == {"bytes": 460, "items": 1, "oldest_at": k["samples"]["oldest_at"], "orphan_items": 0}
    assert k["protection_screenshots"]["items"] == 1 and k["protection_screenshots"]["bytes"] == 900
    assert k["identities"] == {"bytes": 77, "items": 1}
    assert k["other"]["items"] == 1 and k["other"]["bytes"] == 11
    db = k["database"]
    assert db["main_bytes"] > 0 and db["bytes"] == db["main_bytes"] + db["wal_bytes"] + db["shm_bytes"]
    assert body["runs"]["by_state"] == {"queued": 1, "running": 1, "waiting_for_credentials": 1, "stopping": 1, "completed": 2, "failed": 1}
    assert body["runs"]["total"] == 7 and body["runs"]["active"] == 4
    assert body["retention"]["result_days"] == 14 and body["retention"]["interval_seconds"] == 60.0
    assert body["volume"]["total_bytes"] >= body["volume"]["free_bytes"] > 0
    assert body["total_bytes"] == sum(v["bytes"] for v in k.values())
    assert body["last_cleanup"] == {"automatic": None, "automatic_with_removals": None, "manual": None}


async def test_dry_run_removes_nothing_and_apply_removes_the_same(svc):
    from app.jobs.housekeeping import cleanup

    world(svc)
    before = files(svc)
    dry = await cleanup(svc, everything(True))
    _validate("cleanup.schema.json", dry)
    assert files(svc) == before
    assert svc.db.scalar("SELECT COUNT(*) FROM runs") == 7
    assert dry["dry_run"] is True and dry["compaction"]["checkpoint"] == "would_run" and dry["compaction"]["bytes_freed"] is None
    applied = await cleanup(svc, everything(False))
    _validate("cleanup.schema.json", applied)
    assert applied["removed"] == dry["removed"]
    r = applied["removed"]
    assert r["run_working_files"] == {"items": 3, "bytes": 1500}
    assert r["result_files"] == {"items": 1, "bytes": 1000}  # run_done2 is not fetched
    assert r["run_records"]["items"] == 2  # run_done1 and run_failed1; files already counted above
    assert r["samples"] == {"items": 1, "bytes": 460}
    assert r["protection_screenshots"] == {"items": 1, "bytes": 900}
    assert applied["kept"] == {"active_runs": 4, "unfetched_results": 1}
    assert applied["bytes_freed"] >= 1500 + 1000 + 460 + 900
    left = files(svc)
    assert "results/shop.test_run_done2.jsonl" in left and "identities.json" in left and "lost+found/x" in left
    assert not any(f.startswith("samples/smp_a") or f.startswith("samples/shots/") for f in left)
    runs = {r["run_id"] for r in svc.db.all("SELECT run_id FROM runs")}
    assert runs == {"run_done2", "run_active0", "run_active1", "run_active2", "run_active3"}
    assert svc.db.scalar("SELECT COUNT(*) FROM samples") == 0
    # the working rows of run_done2 went with its directory; its records stay (run summary)
    assert svc.db.scalar("SELECT COUNT(*) FROM urls WHERE run_id='run_done2'") == 0
    assert svc.db.scalar("SELECT COUNT(*) FROM records WHERE run_id='run_done2'") == 1


async def test_active_runs_are_never_touched(svc):
    from app.jobs.housekeeping import cleanup

    world(svc)
    await cleanup(svc, everything(False, result_files={"older_than_days": 0, "include_unfetched": True},
                                  run_records={"older_than_days": 0, "include_unfetched": True}))
    left = files(svc)
    for i in range(4):
        assert any(f.startswith(f"jobs/run_active{i}/") for f in left), i
        assert svc.db.scalar("SELECT COUNT(*) FROM urls WHERE run_id=?", (f"run_active{i}",)) == 1
    assert "results/shop.test_run_active1.tmp" in left
    assert svc.db.scalar("SELECT COUNT(*) FROM runs") == 4


async def test_a_finished_run_with_a_live_runner_is_left_alone(svc):
    from app.jobs.housekeeping import cleanup

    add_run(svc, "run_closing", "completed", 5, result_bytes=10, job_bytes=10)
    mark_fetched(svc, "run_closing")
    svc.jobs.runners["run_closing"] = object()  # the runner is still closing
    out = await cleanup(svc, everything(False))
    assert out["removed"]["run_working_files"]["items"] == 0 and out["removed"]["run_records"]["items"] == 0
    assert svc.db.scalar("SELECT COUNT(*) FROM runs") == 1


async def test_unfetched_results_are_kept_unless_allowed(svc):
    from app.jobs.housekeeping import cleanup

    add_run(svc, "run_new", "completed", 1, result_bytes=100)
    body = {"dry_run": False, "result_files": {"older_than_days": 0}}
    out = await cleanup(svc, body)
    assert out["removed"]["result_files"]["items"] == 0 and out["kept"]["unfetched_results"] == 1
    assert (Path(svc.settings.data_dir) / "results" / "shop.test_run_new.jsonl").exists()
    # a handled run_completed event counts as fetched
    svc.db.execute("INSERT INTO events(event_id, run_id, event_type, state, created_at, doc) VALUES ('evt_1','run_new','run_completed','handled',?, '{}')", (iso(now()),))
    out = await cleanup(svc, body)
    assert out["removed"]["result_files"] == {"items": 1, "bytes": 100}
    doc = json.loads(svc.db.scalar("SELECT doc FROM runs WHERE run_id='run_new'"))
    assert doc["result"]["expires_at"] <= iso(now())  # GET …/result now answers result_expired


async def test_include_unfetched_and_age(svc):
    from app.jobs.housekeeping import cleanup

    add_run(svc, "run_young", "completed", 1, result_bytes=100)
    add_run(svc, "run_old", "completed", 9, result_bytes=200)
    out = await cleanup(svc, {"dry_run": False, "result_files": {"older_than_days": 5, "include_unfetched": True}})
    assert out["removed"]["result_files"] == {"items": 1, "bytes": 200} and out["kept"]["unfetched_results"] == 0
    out = await cleanup(svc, {"dry_run": False, "run_records": {"older_than_days": 0}})
    assert out["removed"]["run_records"]["items"] == 1 and out["kept"]["unfetched_results"] == 1  # run_young kept
    assert {r["run_id"] for r in svc.db.all("SELECT run_id FROM runs")} == {"run_young"}


async def test_orphans_go_by_modification_time(svc):
    from app.jobs.housekeeping import cleanup, storage

    data = Path(svc.settings.data_dir)
    _write(data / "jobs" / "run_gone" / "f", 30, 3 * DAY)
    old = time.time() - 3 * DAY
    os.utime(data / "jobs" / "run_gone", (old, old))
    _write(data / "jobs" / "run_busy_orphan" / "f", 30, 3 * DAY)  # a file inside changed today
    _write(data / "jobs" / "run_busy_orphan" / "g", 5)
    _write(data / "results" / "shop.test_run_gone.jsonl", 40, 3 * DAY)
    _write(data / "results" / "shop.test_run_fresh.jsonl", 40)
    _write(data / "samples" / "smp_orphan.html", 50)
    st = await storage(svc)
    assert st["kinds"]["run_working_files"]["orphan_items"] == 2 and st["kinds"]["result_files"]["orphan_items"] == 2
    assert st["kinds"]["samples"]["orphan_items"] == 1
    out = await cleanup(svc, {"dry_run": False, "run_working_files": {"older_than_days": 2}, "result_files": {"older_than_days": 2}, "samples": {"older_than_hours": 0}})
    assert out["removed"]["run_working_files"] == {"items": 1, "bytes": 30}
    assert out["removed"]["result_files"] == {"items": 1, "bytes": 40}
    assert out["removed"]["samples"] == {"items": 1, "bytes": 50}
    left = files(svc)
    assert "results/shop.test_run_fresh.jsonl" in left
    assert "jobs/run_busy_orphan/g" in left
    assert not any(f.startswith("jobs/run_gone") or f == "results/shop.test_run_gone.jsonl" or f.startswith("samples/smp_") for f in left)


async def test_samples_by_age(svc):
    from app.jobs.housekeeping import cleanup

    add_sample(svc, "smp_old", 10, age_hours=10)
    add_sample(svc, "smp_new", 20, age_hours=1)
    _write(Path(svc.settings.data_dir) / "samples" / "shots" / "shot_old.png", 5, 10 * 3600)
    _write(Path(svc.settings.data_dir) / "samples" / "shots" / "shot_new.png", 6, 3600)
    out = await cleanup(svc, {"dry_run": False, "samples": {"older_than_hours": 5}, "protection_screenshots": {"older_than_hours": 5}})
    assert out["removed"]["samples"] == {"items": 1, "bytes": 10}
    assert out["removed"]["protection_screenshots"] == {"items": 1, "bytes": 5}
    assert {r["sample_id"] for r in svc.db.all("SELECT sample_id FROM samples")} == {"smp_new"}


async def test_vacuum_is_skipped_with_an_active_run_and_done_without(svc):
    from app.jobs.housekeeping import cleanup

    for i in range(300):
        svc.db.execute("INSERT INTO records(run_id, record_id, template_id, mapping_version, extracted_at, line) VALUES (?,?,?,?,?,?)",
                       ("run_x", f"r{i}", "t", 1, iso(now()), "y" * 2000))
    svc.db.execute("DELETE FROM records WHERE run_id='run_x'")
    add_run(svc, "run_busy", "running")
    out = await cleanup(svc, {"dry_run": False, "compact_database": True})
    c = out["compaction"]
    assert c["checkpoint"] == "done" and c["vacuum"] == "skipped" and "not finished" in c["vacuum_skipped_reason"]
    assert c["reclaimable_bytes"] > 0 and c["wal_bytes_before"] > 0
    assert c["database_bytes_after"] < c["database_bytes_before"]  # the TRUNCATE checkpoint emptied the WAL
    svc.db.execute("UPDATE runs SET state='completed', finished_at=? WHERE run_id='run_busy'", (iso(now()),))
    dry = await cleanup(svc, {"compact_database": True})
    assert dry["dry_run"] is True and dry["compaction"]["vacuum"] == "would_run" and dry["compaction"]["vacuum_skipped_reason"] is None
    out = await cleanup(svc, {"dry_run": False, "compact_database": True})
    c = out["compaction"]
    assert c["vacuum"] == "done" and c["vacuum_skipped_reason"] is None
    assert c["database_bytes_after"] < c["database_bytes_before"] and c["bytes_freed"] == c["database_bytes_before"] - c["database_bytes_after"]
    assert int(svc.db.scalar("PRAGMA freelist_count")) == 0
    _validate("cleanup.schema.json", out)


async def test_empty_body_is_a_dry_run_that_removes_nothing(svc):
    from app.jobs.housekeeping import cleanup

    world(svc)
    before = files(svc)
    out = await cleanup(svc, {})
    _validate("cleanup.schema.json", out)
    assert out == {"dry_run": True, "time": out["time"], "removed": {}, "kept": {"active_runs": 4, "unfetched_results": 0}, "compaction": None, "bytes_freed": 0}
    assert files(svc) == before


async def test_manual_cleanup_is_recorded(svc):
    from app.jobs.housekeeping import cleanup, storage

    world(svc)
    await cleanup(svc, everything(True))
    assert (await storage(svc))["last_cleanup"]["manual"] is None  # a dry run is not recorded
    await cleanup(svc, everything(False))
    manual = (await storage(svc))["last_cleanup"]["manual"]
    assert manual["trigger"] == "manual" and manual["removed"]["samples"]["items"] == 1 and manual["bytes_freed"] > 0


def test_request_schema_refuses_bad_bodies():
    ref = "cleanup.schema.json#/$defs/cleanup_request"
    assert schema_errors(ref, {"run_records": {"older_than_days": -1}})
    assert schema_errors(ref, {"run_records": {}})
    assert schema_errors(ref, {"identities": True})
    assert schema_errors(ref, {"dry_run": "no"})
    assert not schema_errors(ref, {})
    assert not schema_errors(ref, {"run_records": {"older_than_days": 0, "include_unfetched": True}})


# ---------------------------------------------------------------------- the automatic sweeper
async def test_sweeper_records_its_summary(svc):
    from app.jobs.housekeeping import storage

    s = svc.settings
    add_run(svc, "run_ancient", "completed", s.job_record_days + 1, result_bytes=100, job_bytes=200, expires_days=-1)
    add_run(svc, "run_week", "completed", s.stored_pages_days + 1, result_bytes=300, job_bytes=400)
    add_run(svc, "run_now", "completed", 0, result_bytes=500, job_bytes=600)
    _write(Path(s.data_dir) / "samples" / "shots" / "shot_x.png", 70, (s.samples_hours + 1) * 3600)
    svc.db.execute("INSERT INTO samples(sample_id, site, expires_at, final_url, doc) VALUES ('smp_x','shop.test',?, 'u', '{}')", (iso(now() - timedelta(hours=1)),))
    _write(Path(s.data_dir) / "samples" / "smp_x.html", 80)
    await svc.jobs._retention()
    st = await storage(svc)
    _validate("storage.schema.json", st)
    auto = st["last_cleanup"]["automatic"]
    assert auto["trigger"] == "automatic"
    assert auto["removed"]["run_working_files"] == {"items": 2, "bytes": 600}
    assert auto["removed"]["result_files"] == {"items": 1, "bytes": 100}
    assert auto["removed"]["run_records"] == {"items": 1, "bytes": 0}
    assert auto["removed"]["samples"] == {"items": 1, "bytes": 80}
    assert auto["removed"]["protection_screenshots"] == {"items": 1, "bytes": 70}
    assert auto["bytes_freed"] == 850
    assert st["last_cleanup"]["automatic_with_removals"] == auto
    assert svc.db.scalar("SELECT COUNT(*) FROM urls WHERE run_id='run_week'") == 0  # working rows with the directory
    # a pass that removes nothing updates `automatic` only
    svc.jobs._last_retention = None
    await svc.jobs._retention()
    st2 = await storage(svc)
    assert st2["last_cleanup"]["automatic"]["bytes_freed"] == 0
    assert st2["last_cleanup"]["automatic_with_removals"] == auto


async def test_sweeper_skips_while_a_cleanup_holds_the_lock(svc):
    s = svc.settings
    add_run(svc, "run_week", "completed", s.stored_pages_days + 1, job_bytes=400)
    async with svc.jobs.housekeeping_lock:
        await svc.jobs._retention()
    assert (Path(s.data_dir) / "jobs" / "run_week").exists()
    assert svc.db.scalar("SELECT v FROM meta WHERE k='last_cleanup.automatic'") is None
    await svc.jobs._retention()
    assert not (Path(s.data_dir) / "jobs" / "run_week").exists()


async def test_cleanup_waits_for_the_sweeper(svc):
    from app.jobs.housekeeping import cleanup

    order = []
    async with svc.jobs.housekeeping_lock:
        task = asyncio.create_task(cleanup(svc, {}))
        await asyncio.sleep(0.05)
        order.append("lock released")
        assert not task.done()
    await task
    order.append("cleanup done")
    assert order == ["lock released", "cleanup done"]
