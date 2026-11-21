"""Queued reasons describe current scheduler blockers, never idle capacity."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.db import Database, dumps
from app.jobs.manager import JobManager


@pytest.fixture
def manager(tmp_path):
    db = Database(tmp_path / "jobs.sqlite")
    svc = SimpleNamespace(
        db=db,
        settings=SimpleNamespace(limits={"max_concurrent_jobs": 2}, compare_drop_share=0.3, job_record_days=14),
        events=SimpleNamespace(for_run=lambda _: [], pending_count=lambda _: 0),
        secrets=SimpleNamespace(present=lambda *_: False),
    )
    yield JobManager(svc)
    db.close()


def add_run(manager, run_id, site, state):
    doc = json.loads((Path(__file__).parents[2] / "contract/examples/get_run.response.json").read_text())
    doc.update(run_id=run_id, site=site, state=state, outcome=None, stop_reason=None, result=None, finished_at=None)
    doc.pop("queued_reason", None)
    manager.svc.db.execute(
        "INSERT INTO runs(run_id,idempotency_key,fingerprint,state,site,created_at,updated_at,doc,runtime) VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, run_id, "f", state, site, doc["created_at"], doc["updated_at"], dumps(doc), "{}"),
    )
    if state in ("running", "stopping"):
        manager.runners[run_id] = SimpleNamespace(state=state, doc=doc, rt={})


def assert_reason(manager, want):
    summary = manager.summary("run_queued")
    row = manager.svc.db.one("SELECT * FROM runs WHERE run_id='run_queued'")
    item = manager.list_item(row)
    for result in (summary, item):
        if want is None:
            assert "queued_reason" not in result
        else:
            assert result["queued_reason"]["code"] == want
    return summary


def test_idle_queued_run_has_no_capacity_blocker(manager):
    add_run(manager, "run_queued", "target.test", "queued")
    assert_reason(manager, None)


def test_one_occupied_slot_does_not_exhaust_capacity(manager):
    add_run(manager, "run_queued", "target.test", "queued")
    add_run(manager, "run_a", "a.test", "running")
    assert_reason(manager, None)


def test_full_capacity_reason_clears_when_a_slot_is_free(manager):
    add_run(manager, "run_queued", "target.test", "queued")
    add_run(manager, "run_a", "a.test", "running")
    add_run(manager, "run_b", "b.test", "stopping")
    assert_reason(manager, "no_free_slot")
    manager.runners["run_a"].state = "completed"
    manager.svc.db.execute("UPDATE runs SET state='completed' WHERE run_id='run_a'")
    assert_reason(manager, None)


@pytest.mark.parametrize("state", ["running", "stopping"])
def test_same_site_blocker_takes_precedence_over_full_capacity(manager, state):
    add_run(manager, "run_queued", "target.test", "queued")
    add_run(manager, "run_a", "target.test", state)
    add_run(manager, "run_b", "b.test", "running")
    summary = assert_reason(manager, "same_site_running")
    assert summary["queued_reason"]["run_id"] == "run_a"


@pytest.mark.parametrize("state", ["running", "completed", "failed"])
def test_queue_reason_is_absent_after_promotion_or_finish(manager, state):
    add_run(manager, "run_queued", "target.test", "queued")
    add_run(manager, "run_a", "a.test", "running")
    add_run(manager, "run_b", "b.test", "stopping")
    assert_reason(manager, "no_free_slot")
    manager.svc.db.execute("UPDATE runs SET state=? WHERE run_id='run_queued'", (state,))
    assert_reason(manager, None)


@pytest.mark.parametrize("state", ["queued", "running", "completed", "failed"])
def test_old_document_queue_reason_is_not_reused(manager, state):
    add_run(manager, "run_queued", "target.test", state)
    row = manager.svc.db.one("SELECT doc FROM runs WHERE run_id='run_queued'")
    doc = json.loads(row["doc"])
    doc["queued_reason"] = {"code": "no_free_slot", "run_id": None, "message": "All slots were busy"}
    manager.svc.db.execute("UPDATE runs SET doc=? WHERE run_id='run_queued'", (dumps(doc),))
    if "run_queued" in manager.runners:
        manager.runners["run_queued"].doc = doc
    assert_reason(manager, None)
