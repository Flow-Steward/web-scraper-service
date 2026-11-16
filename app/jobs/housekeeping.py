"""Disk use of the data volume and clean-up (contract README 16).

``GET /v1/admin/storage`` (:func:`storage`), ``POST /v1/admin/cleanup`` (:func:`cleanup`) and
the summary the automatic sweeper (``JobManager._retention``) records after each pass.

Rules:

* a run that is not finished (``queued``, ``running``, ``waiting_for_credentials``,
  ``stopping``) or still has a runner in memory is never touched;
* ``identities.json`` and unknown files under the data directory are never touched;
* a result file the platform has not fetched yet is kept unless the request allows it.
  "Fetched" = the service sent the file's last byte in a download answer, or the run's
  ``run_completed`` event is ``handled``. A run's result files are its JSON Lines and CSV
  result (README 7.5); they go together;
* the file walk and the deletions run in a worker thread; every database statement runs on
  the event loop (``app.core.db``: one connection, used from the loop only);
* the clean-up and the sweeper hold ``JobManager.housekeeping_lock``; the sweeper skips a pass
  while a clean-up holds it.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.core.db import dumps, loads
from app.core.timeutil import iso, now, now_iso, parse
from app.core.urls import run_file_names

if TYPE_CHECKING:
    from app.service import Service

ACTIVE_STATES = ("queued", "running", "waiting_for_credentials", "stopping")
FINAL_STATES = ("completed", "failed")
RUN_STATES = ACTIVE_STATES + FINAL_STATES
REMOVABLE_KINDS = ("run_working_files", "result_files", "run_records", "samples", "protection_screenshots")
DB_NAME = "service.sqlite3"
IDENTITIES_NAME = "identities.json"
KNOWN_TOP = {"jobs", "results", "samples", DB_NAME, f"{DB_NAME}-wal", f"{DB_NAME}-shm", IDENTITIES_NAME}
RUN_TABLES = ("records", "unextracted", "stored_pages", "urls", "link_backlog", "mapping_requests", "events", "run_secrets", "file_fetches")
WORKING_TABLES = ("stored_pages", "urls", "link_backlog")
META_KEYS = {"automatic": "last_cleanup.automatic", "automatic_with_removals": "last_cleanup.automatic_with_removals", "manual": "last_cleanup.manual"}


# ---------------------------------------------------------------------- file sizes
def tree_bytes(path: Path) -> int:
    """Apparent size of a file or of every file under a directory (symlinks not followed)."""
    return tree_stat(path)[0]


def tree_stat(path: Path) -> tuple[int, float]:
    """(apparent size, newest modification time) of a file or a directory tree."""
    try:
        st = path.lstat()
    except OSError:
        return 0, 0.0
    if not path.is_dir() or path.is_symlink():
        return st.st_size, st.st_mtime
    total = 0
    newest = st.st_mtime
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        est = entry.stat(follow_symlinks=False)
                        newest = max(newest, est.st_mtime)
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        else:
                            total += est.st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total, newest


@dataclass
class Item:
    path: Path
    bytes: int
    mtime: float  # a directory: the newest modification time inside it


def _entries(directory: Path, *, sizes: bool = True) -> list[Item]:
    out: list[Item] = []
    try:
        with os.scandir(directory) as it:
            for entry in it:
                p = Path(entry.path)
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if sizes and entry.is_dir(follow_symlinks=False):
                    out.append(Item(p, *tree_stat(p)))  # a directory: its newest change
                else:
                    out.append(Item(p, st.st_size, st.st_mtime))
    except OSError:
        pass
    return out


def _ts(mtime: float | None) -> str | None:
    return None if mtime is None else iso(datetime.fromtimestamp(mtime, UTC))


@dataclass
class Scan:
    jobs: list[Item] = field(default_factory=list)
    results: list[Item] = field(default_factory=list)
    samples: list[Item] = field(default_factory=list)  # files directly under samples/
    shots: list[Item] = field(default_factory=list)
    other: list[Item] = field(default_factory=list)
    db_files: dict[str, int] = field(default_factory=dict)
    identities: int | None = None


def scan(data: Path) -> Scan:
    """One walk of the data directory (worker thread)."""
    s = Scan()
    s.jobs = _entries(data / "jobs")
    s.results = [i for i in _entries(data / "results") if i.path.is_file()]
    s.samples = [i for i in _entries(data / "samples", sizes=False) if i.path.is_file()]
    s.shots = [i for i in _entries(data / "samples" / "shots", sizes=False) if i.path.is_file()]
    for name in (DB_NAME, f"{DB_NAME}-wal", f"{DB_NAME}-shm"):
        try:
            s.db_files[name] = (data / name).stat().st_size
        except OSError:
            s.db_files[name] = 0
    try:
        s.identities = (data / IDENTITIES_NAME).stat().st_size
    except OSError:
        s.identities = None
    try:
        with os.scandir(data) as it:
            others = [Path(e.path) for e in it if e.name not in KNOWN_TOP]
    except OSError:
        others = []
    s.other = [Item(p, *tree_stat(p)) for p in others]
    return s


def _sample_id(name: str) -> str:
    for suffix in (".extra.json", ".html"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


# ---------------------------------------------------------------------- runs
@dataclass
class RunInfo:
    run_id: str
    state: str
    finished_at: datetime | None
    doc: dict
    file_names: list[str]
    fetched: bool


def _runs(svc: "Service") -> dict[str, RunInfo]:
    db = svc.db
    fetched = {r["run_id"] for r in db.all("SELECT run_id FROM file_fetches WHERE kind='result'")}
    fetched |= {r["run_id"] for r in db.all("SELECT DISTINCT run_id FROM events WHERE event_type='run_completed' AND state='handled'")}
    out: dict[str, RunInfo] = {}
    for row in db.all("SELECT run_id, state, site, finished_at, doc FROM runs"):
        doc = loads(row["doc"])
        names = list(run_file_names(row["site"], row["run_id"]).values())
        result = (doc.get("result") or {}).get("file_name")
        if result and result not in names:
            names.insert(0, result)
        out[row["run_id"]] = RunInfo(row["run_id"], row["state"], parse(row["finished_at"]), doc, names, row["run_id"] in fetched)
    return out


def _active(svc: "Service", runs: dict[str, RunInfo]) -> set[str]:
    return {r.run_id for r in runs.values() if r.state not in FINAL_STATES} | set(svc.jobs.runners)


def _result_owners(runs: dict[str, RunInfo]) -> dict[str, str]:
    """File name (and the ``.tmp`` files written before it) -> run_id."""
    owners: dict[str, str] = {}
    for r in runs.values():
        for name in r.file_names:
            owners[name] = r.run_id
            owners[name + ".tmp"] = r.run_id
            owners[str(Path(name).with_suffix(".tmp"))] = r.run_id
    return owners


def database_numbers(svc: "Service") -> dict:
    page_size = int(svc.db.scalar("PRAGMA page_size") or 0)
    free_pages = int(svc.db.scalar("PRAGMA freelist_count") or 0)
    return {"reclaimable_bytes": page_size * free_pages}


# ---------------------------------------------------------------------- summaries
def new_summary(trigger: str) -> dict:
    return {"at": now_iso(), "trigger": trigger, "removed": {k: {"items": 0, "bytes": 0} for k in REMOVABLE_KINDS}, "bytes_freed": 0}


def add(summary: dict, kind: str, items: int, size: int) -> None:
    summary["removed"][kind]["items"] += items
    summary["removed"][kind]["bytes"] += size
    summary["bytes_freed"] += size


def save_summary(svc: "Service", summary: dict) -> None:
    key = META_KEYS["manual"] if summary["trigger"] == "manual" else META_KEYS["automatic"]
    rows = [(key, dumps(summary))]
    if summary["trigger"] == "automatic" and any(v["items"] for v in summary["removed"].values()):
        rows.append((META_KEYS["automatic_with_removals"], dumps(summary)))
    for k, v in rows:
        svc.db.execute("INSERT OR REPLACE INTO meta(k, v) VALUES (?, ?)", (k, v))


def last_cleanups(svc: "Service") -> dict:
    out = {}
    for name, key in META_KEYS.items():
        out[name] = loads(svc.db.scalar("SELECT v FROM meta WHERE k=?", (key,)))
    return out


# ---------------------------------------------------------------------- storage
def _kind(items: list[Item]) -> dict:
    return {
        "bytes": sum(i.bytes for i in items),
        "items": len(items),
        "oldest_at": _ts(min((i.mtime for i in items), default=None)),
    }


async def storage(svc: "Service") -> dict:
    s = svc.settings
    data = Path(s.data_dir)
    sc = await asyncio.to_thread(scan, data)
    runs = _runs(svc)
    active = _active(svc, runs)
    owners = _result_owners(runs)

    jobs = _kind(sc.jobs)
    jobs["active_items"] = sum(1 for i in sc.jobs if i.path.name in active)
    jobs["orphan_items"] = sum(1 for i in sc.jobs if i.path.name not in runs)

    results = _kind(sc.results)
    unfetched = [i for i in sc.results if (o := owners.get(i.path.name)) and o not in active and runs[o].state == "completed" and not runs[o].fetched]
    results["unfetched_items"] = len(unfetched)
    results["unfetched_bytes"] = sum(i.bytes for i in unfetched)
    results["active_items"] = sum(1 for i in sc.results if owners.get(i.path.name) in active)
    results["orphan_items"] = sum(1 for i in sc.results if i.path.name not in owners)

    sample_rows = {r["sample_id"] for r in svc.db.all("SELECT sample_id FROM samples")}
    groups: dict[str, list[Item]] = {}
    for item in sc.samples:
        groups.setdefault(_sample_id(item.path.name), []).append(item)
    samples = {
        "bytes": sum(i.bytes for i in sc.samples),
        "items": len(groups.keys() | sample_rows),
        "oldest_at": _ts(min((i.mtime for i in sc.samples), default=None)),
        "orphan_items": sum(1 for sid in groups if sid not in sample_rows),
    }

    db_bytes = sum(sc.db_files.values())
    database = {
        "bytes": db_bytes,
        "main_bytes": sc.db_files[DB_NAME],
        "wal_bytes": sc.db_files[f"{DB_NAME}-wal"],
        "shm_bytes": sc.db_files[f"{DB_NAME}-shm"],
        **database_numbers(svc),
    }
    identities = {"bytes": sc.identities or 0, "items": 0 if sc.identities is None else 1}
    other = _kind(sc.other)
    shots = _kind(sc.shots)

    counts = {state: 0 for state in RUN_STATES}
    for row in svc.db.all("SELECT state, COUNT(*) AS n FROM runs GROUP BY state"):
        counts[row["state"]] = int(row["n"])
    oldest_finished = svc.db.scalar("SELECT MIN(finished_at) FROM runs WHERE state IN ('completed','failed')")

    usage = shutil.disk_usage(data)
    kinds = {
        "run_working_files": jobs,
        "result_files": results,
        "samples": samples,
        "protection_screenshots": shots,
        "database": database,
        "identities": identities,
        "other": other,
    }
    return {
        "time": now_iso(),
        "data_dir": str(data),
        "volume": {"total_bytes": usage.total, "free_bytes": usage.free, "used_bytes": usage.used},
        "total_bytes": sum(k["bytes"] for k in kinds.values()),
        "kinds": kinds,
        "runs": {"by_state": counts, "total": sum(counts.values()), "active": len(active), "oldest_finished_at": oldest_finished},
        "retention": {
            "result_days": s.result_days,
            "job_record_days": s.job_record_days,
            "stored_pages_days": s.stored_pages_days,
            "samples_hours": s.samples_hours,
            "interval_seconds": s.retention_interval_seconds,
        },
        "last_cleanup": last_cleanups(svc),
    }


# ---------------------------------------------------------------------- clean-up
@dataclass
class Plan:
    kind: str
    path: Path | None
    bytes: int
    run_id: str | None = None
    sample_id: str | None = None


def _remove_paths(paths: list[Path]) -> None:
    for p in paths:
        try:
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
        except OSError:
            pass


async def cleanup(svc: "Service", body: dict) -> dict:
    async with svc.jobs.housekeeping_lock:
        return await _cleanup(svc, body)


async def _cleanup(svc: "Service", body: dict) -> dict:
    s = svc.settings
    data = Path(s.data_dir)
    dry = bool(body.get("dry_run", True))
    ts = now()
    sc = await asyncio.to_thread(scan, data)
    runs = _runs(svc)
    active = _active(svc, runs)
    owners = _result_owners(runs)
    plan: list[Plan] = []
    planned: set[Path] = set()
    kept_unfetched: set[str] = set()

    def queue(p: Plan) -> None:
        if p.path is not None:
            if p.path in planned:
                p.bytes = 0
                p.path = None
            else:
                planned.add(p.path)
        plan.append(p)

    def old_enough(when: datetime | None, cutoff: datetime) -> bool:
        return when is not None and when <= cutoff

    def unfetched(run_id: str) -> bool:
        r = runs.get(run_id)
        return r is not None and r.state == "completed" and not r.fetched

    # finished runs' working files (jobs/<run_id>; orphans by modification time)
    if (opt := body.get("run_working_files")) is not None:
        cutoff = ts - timedelta(days=opt["older_than_days"])
        for item in sc.jobs:
            run_id = item.path.name
            if run_id in active:
                continue
            r = runs.get(run_id)
            when = r.finished_at if r else datetime.fromtimestamp(item.mtime, UTC)
            if old_enough(when, cutoff):
                queue(Plan("run_working_files", item.path, item.bytes, run_id if r else None))

    # result files (results/*; orphans by modification time)
    if (opt := body.get("result_files")) is not None:
        cutoff = ts - timedelta(days=opt["older_than_days"])
        allow = bool(opt.get("include_unfetched", False))
        for item in sc.results:
            owner = owners.get(item.path.name)
            if owner in active:
                continue
            r = runs.get(owner) if owner else None
            when = r.finished_at if r else datetime.fromtimestamp(item.mtime, UTC)
            if not old_enough(when, cutoff):
                continue
            if r is not None and unfetched(r.run_id) and not allow:
                kept_unfetched.add(r.run_id)
                continue
            queue(Plan("result_files", item.path, item.bytes, r.run_id if r else None))

    # whole run records (rows of every table, the run's directory and result file)
    if (opt := body.get("run_records")) is not None:
        cutoff = ts - timedelta(days=opt["older_than_days"])
        allow = bool(opt.get("include_unfetched", False))
        results_by_name = {i.path.name: i for i in sc.results}
        jobs_by_name = {i.path.name: i for i in sc.jobs}
        for r in runs.values():
            if r.run_id in active or r.state not in FINAL_STATES or not old_enough(r.finished_at, cutoff):
                continue
            items = [results_by_name[n] for n in r.file_names if n in results_by_name]
            if items and unfetched(r.run_id) and not allow:
                kept_unfetched.add(r.run_id)
                continue
            queue(Plan("run_records", None, 0, r.run_id))
            if (job := jobs_by_name.get(r.run_id)) is not None:
                queue(Plan("run_records", job.path, job.bytes, r.run_id))
            for item in items:
                queue(Plan("run_records", item.path, item.bytes, r.run_id))

    # samples (files + rows; files without a row by modification time)
    if (opt := body.get("samples")) is not None:
        cutoff = ts - timedelta(hours=opt["older_than_hours"])
        groups: dict[str, list[Item]] = {}
        for item in sc.samples:
            groups.setdefault(_sample_id(item.path.name), []).append(item)
        rows = {r["sample_id"] for r in svc.db.all("SELECT sample_id FROM samples")}
        for sid in sorted(groups.keys() | rows):
            files = groups.get(sid, [])
            newest = max((i.mtime for i in files), default=None)
            if newest is not None and datetime.fromtimestamp(newest, UTC) > cutoff:
                continue
            queue(Plan("samples", None, 0, sample_id=sid))
            for f in files:
                queue(Plan("samples", f.path, f.bytes, sample_id=sid))

    if (opt := body.get("protection_screenshots")) is not None:
        cutoff = ts - timedelta(hours=opt["older_than_hours"])
        for item in sc.shots:
            if datetime.fromtimestamp(item.mtime, UTC) <= cutoff:
                queue(Plan("protection_screenshots", item.path, item.bytes))

    removed = {k: {"items": 0, "bytes": 0} for k in REMOVABLE_KINDS if body.get(k) is not None}
    for kind in removed:
        entries = [p for p in plan if p.kind == kind]
        if kind == "run_records":
            removed[kind]["items"] = len({p.run_id for p in entries})
        elif kind == "samples":
            removed[kind]["items"] = len({p.sample_id for p in entries})
        else:
            removed[kind]["items"] = len(entries)
        removed[kind]["bytes"] = sum(p.bytes for p in entries)

    if not dry:
        await asyncio.to_thread(_remove_paths, [p.path for p in plan if p.path is not None])
        _apply_rows(svc, plan)

    compaction = None
    if body.get("compact_database"):
        compaction = await _compact(svc, dry)

    bytes_freed = sum(v["bytes"] for v in removed.values()) + ((compaction or {}).get("bytes_freed") or 0)
    out = {
        "dry_run": dry,
        "time": iso(ts),
        "removed": removed,
        "kept": {"active_runs": len(active), "unfetched_results": len(kept_unfetched)},
        "compaction": compaction,
        "bytes_freed": bytes_freed,
    }
    if not dry:
        summary = new_summary("manual")
        summary["at"] = iso(ts)
        for kind, v in removed.items():
            add(summary, kind, v["items"], v["bytes"])
        if compaction and compaction.get("bytes_freed"):
            summary["bytes_freed"] += compaction["bytes_freed"]
        save_summary(svc, summary)
    return out


def _apply_rows(svc: "Service", plan: list[Plan]) -> None:
    db = svc.db
    ts = now_iso()
    with db.tx():
        for p in plan:
            if p.kind == "run_working_files" and p.run_id:
                for table in WORKING_TABLES:
                    db.execute(f"DELETE FROM {table} WHERE run_id=?", (p.run_id,))
            elif p.kind == "result_files" and p.run_id:
                # the descriptor follows the file: GET …/result answers 410 result_expired
                row = db.one("SELECT doc FROM runs WHERE run_id=?", (p.run_id,))
                if row is not None:
                    doc = loads(row["doc"])
                    if doc.get("result") and parse(doc["result"]["expires_at"]) > now():
                        doc["result"]["expires_at"] = ts
                        db.execute("UPDATE runs SET doc=? WHERE run_id=?", (dumps(doc), p.run_id))
            elif p.kind == "run_records" and p.path is None and p.run_id:
                for table in RUN_TABLES:
                    db.execute(f"DELETE FROM {table} WHERE run_id=?", (p.run_id,))
                db.execute("DELETE FROM runs WHERE run_id=? AND state IN ('completed','failed')", (p.run_id,))
            elif p.kind == "samples" and p.path is None and p.sample_id:
                db.execute("DELETE FROM samples WHERE sample_id=?", (p.sample_id,))


def _db_bytes(svc: "Service") -> tuple[int, int]:
    path = Path(svc.db.path)
    total = 0
    wal = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            size = Path(str(path) + suffix).stat().st_size
        except OSError:
            size = 0
        total += size
        if suffix == "-wal":
            wal = size
    return total, wal


async def _compact(svc: "Service", dry: bool) -> dict:
    db = svc.db
    before, wal_before = _db_bytes(svc)
    reclaimable = database_numbers(svc)["reclaimable_bytes"]
    runs = _runs(svc)
    active = _active(svc, runs)
    main_bytes = before - wal_before
    reason = None
    if active:
        reason = f"{len(active)} run(s) not finished; VACUUM runs only when no run is queued, running, waiting or stopping"
    else:
        free = shutil.disk_usage(Path(svc.settings.data_dir)).free
        if free < main_bytes * 2:
            reason = f"free disk ({free} bytes) is less than twice the database ({main_bytes} bytes); VACUUM needs room for a copy"
    out: dict[str, Any] = {
        "checkpoint": "would_run" if dry else "done",
        "vacuum": "skipped" if reason else ("would_run" if dry else "done"),
        "vacuum_skipped_reason": reason,
        "database_bytes_before": before,
        "wal_bytes_before": wal_before,
        "reclaimable_bytes": reclaimable,
        "database_bytes_after": None,
        "bytes_freed": None,
    }
    if dry:
        return out
    row = db.one("PRAGMA wal_checkpoint(TRUNCATE)")
    if row is not None and int(row[0]) != 0:
        out["checkpoint"] = "busy"
    if reason is None:
        db.execute("VACUUM")
        db.one("PRAGMA wal_checkpoint(TRUNCATE)")
    after, _ = _db_bytes(svc)
    out["database_bytes_after"] = after
    out["bytes_freed"] = max(0, before - after)
    return out
