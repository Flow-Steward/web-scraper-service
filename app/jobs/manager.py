"""Job manager: creation with idempotency, the slot scheduler, stop/resume, restart recovery,
completion and failure (README 4, 5, 10)."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.core.config import apply_rules, dedupe_url_list, needs_proxy_or_solver_secrets, profile_summary, secret_part, selected_tiers
from app.core.db import dumps, loads
from app.core.errors import ApiError, field_error, validation_failed
from app.core.ids import new_id
from app.core.timeutil import iso, now, now_iso, parse, plus
from app.core.urls import canonical_url, result_file_name, run_file_names, site_key

if TYPE_CHECKING:
    from app.jobs.runner import JobRunner
    from app.service import Service

log = logging.getLogger("wss.jobs")


def _canon_numbers(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {k: _canon_numbers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_canon_numbers(v) for v in value]
    return value


def fingerprint(config: dict, platform: dict) -> str:
    text = json.dumps(_canon_numbers({"config": config, "platform": platform}), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def empty_counters() -> dict:
    return {
        "urls_discovered": 0,
        "urls_skipped": {"filtered": 0, "robots": 0, "private_address": 0, "duplicate": 0},
        "pages_fetched": 0,
        "pages_by_class": {},
        "http_fetches": 0,
        "browser_fetches": 0,
        "retries": 0,
        "records_extracted": 0,
        "unextracted": 0,
        "stored_pages": 0,
        "refetch_after_repair": 0,
        "soft_errors": 0,
        "soft_errors_by_reason": {},
        "repairs_avoided": 0,
        "repairs_started": 0,
        "repairs_succeeded": 0,
        "proxy_requests_by_tier": {},
        "current_proxy_tier": None,
        "result_bytes": 0,
        "required_fill_rate": None,
        "links_skipped": {},
    }


# unextracted reasons a re-collect takes by default (README 4.10): not 404s and not refusals
DEFAULT_RECOLLECT_REASONS = ("fetch_failed", "blocked", "incomplete", "no_template", "template_failed", "stopped")


class JobManager:
    def __init__(self, svc: "Service") -> None:
        self.svc = svc
        self.runners: dict[str, "JobRunner"] = {}
        self._create_lock = asyncio.Lock()
        self._sched_task: asyncio.Task | None = None
        self._stopping = False
        self._wake = asyncio.Event()
        # held by the automatic sweeper and POST /v1/admin/cleanup (never both at once)
        self.housekeeping_lock = asyncio.Lock()

    # ================================================================== start-up / shutdown
    async def start(self) -> None:
        await self._recover()
        self._sched_task = asyncio.create_task(self._scheduler(), name="job-scheduler")

    async def shutdown(self) -> None:
        self._stopping = True
        if self._sched_task:
            self._sched_task.cancel()
        runners = list(self.runners.values())
        # all runners in parallel, inside the 90 s grace period
        await asyncio.gather(*(r.shutdown(budget=45.0) for r in runners), return_exceptions=True)

    async def _recover(self) -> None:
        db = self.svc.db
        rows = db.all("SELECT run_id, state, runtime FROM runs WHERE state IN ('running','stopping') ORDER BY created_at")
        for row in rows:
            run_id = row["run_id"]
            rt = loads(row["runtime"])
            ts = now()
            restarts = [r for r in rt.get("restarts", []) if (ts - parse(r)).total_seconds() < 600]
            restarts.append(iso(ts))
            rt["restarts"] = restarts
            # time between the crash and now does not count as running time
            rt["running_since"] = None
            db.execute("UPDATE runs SET runtime=? WHERE run_id=?", (dumps(rt), run_id))
            if len(restarts) >= 3:
                doc = loads(db.one("SELECT doc FROM runs WHERE run_id=?", (run_id,))["doc"])
                await self._fail_doc(run_id, doc, rt, "crash_loop", "the service restarted 3 times within 10 minutes while the job was running")
                continue
            if rt.get("needs_secrets") and not self.svc.secrets.readable(run_id, "proxy_solver"):
                if row["state"] == "running":
                    await self.to_waiting_for_credentials(run_id)
                    continue
            self._start_runner(run_id)
        # events of finished jobs continue by themselves (delivery worker)

    async def _scheduler(self) -> None:
        while not self._stopping:
            try:
                await self._promote()
                await self._retention()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scheduler step failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    def wake(self) -> None:
        self._wake.set()

    async def _promote(self) -> None:
        """Queued runs start in order of creation while slots are free; a run of a site that has a
        run ``running`` or ``stopping`` waits until that run ends, later runs of other sites
        may pass it."""
        if self._stopping:
            return
        cap = self.svc.settings.limits["max_concurrent_jobs"]
        active = sum(1 for r in self.runners.values() if r.state in ("running", "stopping"))
        if active >= cap:
            return
        busy = {r["site"] for r in self.svc.db.all("SELECT site FROM runs WHERE state IN ('running','stopping')")}
        for row in self.svc.db.all("SELECT run_id, site FROM runs WHERE state='queued' ORDER BY created_at, run_id"):
            if active >= cap:
                return
            if row["site"] in busy:
                continue
            self._set_state(row["run_id"], "running")
            self._start_runner(row["run_id"])
            busy.add(row["site"])
            active += 1

    def _refuse_if_site_busy(self, cfg: dict) -> None:
        """``when_site_busy: skip`` — a start while a run of the same site is queued, running or
        stopping is refused (409 ``site_busy``); nothing is stored."""
        try:
            if cfg.get("discovery_mode") == "url_list":
                site = site_key(cfg["urls"][0]["url"])
            else:
                site = site_key(cfg["site_url"])
        except (ValueError, KeyError, IndexError, TypeError):
            return
        row = self.svc.db.one("SELECT run_id, state, created_at, doc FROM runs WHERE site=? AND state NOT IN ('completed','failed') "
                              "ORDER BY created_at LIMIT 1", (site,))
        if row is None:
            return
        other = loads(row["doc"])
        since = other.get("started_at") or row["created_at"]
        raise ApiError("site_busy", f"A run of {site} is already {row['state']} since {since} (run {row['run_id']}); this start was skipped.",
                       details={"run_id": row["run_id"], "state": row["state"], "started_at": other.get("started_at"),
                                "created_at": row["created_at"], "site": site})

    def queued_reason(self, run_id: str, site: str) -> dict | None:
        """Why a queued run has not started (README 4.13)."""
        other = self.svc.db.one("SELECT run_id FROM runs WHERE site=? AND state IN ('running','stopping') AND run_id != ? ORDER BY created_at LIMIT 1",
                                (site, run_id))
        if other is not None:
            return {"code": "same_site_running", "run_id": other["run_id"],
                    "message": f"another run of this site is running ({other['run_id']}); this run starts when it ends"}
        cap = self.svc.settings.limits["max_concurrent_jobs"]
        active = sum(1 for r in self.runners.values() if r.state in ("running", "stopping"))
        if active < cap:
            return None  # waiting for the next scheduler tick, not blocked by capacity
        return {"code": "no_free_slot", "run_id": None,
                "message": f"all {cap} run slots are busy (max_concurrent_jobs); this run starts when one is free"}

    def _set_state(self, run_id: str, state: str, extra: dict | None = None) -> dict:
        with self.svc.db.tx():
            row = self.svc.db.one("SELECT doc FROM runs WHERE run_id=?", (run_id,))
            doc = loads(row["doc"])
            doc["state"] = state
            doc["updated_at"] = now_iso()
            for k, v in (extra or {}).items():
                doc[k] = v
            self.svc.db.execute("UPDATE runs SET state=?, updated_at=?, doc=? WHERE run_id=?", (state, doc["updated_at"], dumps(doc), run_id))
        return doc

    def _start_runner(self, run_id: str) -> None:
        from app.jobs.runner import JobRunner

        if run_id in self.runners or self._stopping:
            return
        runner = JobRunner(self.svc, run_id)
        self.runners[run_id] = runner
        runner.start()

    def runner_done(self, run_id: str) -> None:
        self.runners.pop(run_id, None)
        # Stop can arrive after a paused runner left its supervisor but while it
        # still owns the queue. Finish stopping only after that owner exits.
        row = self.svc.db.one("SELECT state FROM runs WHERE run_id=?", (run_id,))
        if row is not None and row["state"] == "stopping":
            self._start_runner(run_id)
        self.wake()

    # ================================================================== creation
    async def create(self, body: dict, idem_key: str, recollect_of: dict | None = None,
                     sitemap_retry: dict | None = None, queue_retry: list[dict] | None = None) -> tuple[int, dict]:
        svc = self.svc
        config, profile, platform = body["config"], body["profile"], body["platform"]
        fingerprint_config = config
        if recollect_of is not None:
            fingerprint_config = {"config": config, "recollect": {"run_id": recollect_of["run_id"], "reasons": recollect_of["reasons"]}}
        fp = fingerprint(fingerprint_config, platform)
        async with self._create_lock:
            row = svc.db.one("SELECT run_id, fingerprint FROM runs WHERE idempotency_key=?", (idem_key,))
            if row is not None:
                if row["fingerprint"] == fp:
                    return 200, self.summary(row["run_id"])
                raise ApiError("idempotency_key_reused", "the Idempotency-Key was used with another body", details={"run_id": row["run_id"]},
                               headers={"Location": f"/v1/runs/{row['run_id']}"})
            if not svc.secrets.configured:
                raise ApiError("secrets_key_unavailable", "the service has no job-secret encryption key configured", headers={"Retry-After": "300"})
            from app.fetching.browsers import browser_presence, resolve_browser

            fixed, errors, _warnings = await apply_rules(config, profile, svc.settings, browser_presence())
            if errors:
                raise validation_failed(errors)
            if fixed.get("when_site_busy", "skip") == "skip":
                self._refuse_if_site_busy(fixed)
            queued = int(svc.db.scalar("SELECT COUNT(*) FROM runs WHERE state='queued'") or 0)
            if queued >= svc.settings.limits["max_queued_jobs"]:
                raise ApiError("queue_full", "too many queued jobs", headers={"Retry-After": "60"})
            run_id = new_id("run")
            ts = now_iso()
            mode = fixed["discovery_mode"]
            if mode == "url_list":
                sites: list[str] = []
                for entry in fixed["urls"]:
                    s = site_key(entry["url"])
                    if s not in sites:
                        sites.append(s)
                site = sites[0]
            else:
                site = site_key(fixed["site_url"])
                sites = [site]
            summary_profile = profile_summary(profile)
            has_secret_part = bool(profile.get("proxy_tiers")) or bool(profile.get("solver"))
            prot_solver = profile.get("solver") or {}
            doc = {
                "run_id": run_id,
                "state": "queued",
                "outcome": None,
                "stop_reason": None,
                "site": site,
                "sites": sites,
                "created_at": ts,
                "started_at": None,
                "updated_at": ts,
                "finished_at": None,
                "expires_at": None,
                "platform": platform,
                "idempotency_key": idem_key,
                "config": fixed,
                "resolved": {"discovery_mode": None, "browser": resolve_browser(fixed["browser"]), "reasons": []},
                "profile": summary_profile,
                "secrets": {"proxy_and_solver": "kept" if has_secret_part else "not_configured", "signing_secret": "kept"},
                "progress": {"discovery": "pending", "queue_pending": 0, "in_progress": 0, "open_mapping_requests": 0, "pending_reextraction": 0, "result_status": "not_started"},
                "counters": empty_counters(),
                "protection": {
                    "challenges_met": 0, "passed_by_step": {}, "frame_not_found": 0, "failed": 0, "hard_blocks": 0,
                    "clicks_used": 0, "clicks_limit": fixed["max_clicks_per_job"],
                    "solves_used": 0, "solves_limit": int(prot_solver.get("max_solves_per_job", 20)) if prot_solver else 0,
                    "sessions_retired": 0,
                },
                "errors": [],
                "error": None,
                "result": None,
            }
            if recollect_of:
                doc["recollect_of"] = recollect_of
            runtime = {"needs_secrets": needs_proxy_or_solver_secrets(fixed, profile), "templates": {}, "restarts": [], "running_seconds": 0.0}
            if sitemap_retry is not None:
                runtime.update(sitemap_traversal=sitemap_retry["traversal"],
                               sitemap_retry_urls=sitemap_retry["urls"], sitemap_retry_roots=sitemap_retry["roots"])
            if queue_retry is not None:
                runtime.update(recollect_pending=queue_retry, start_fed=True)
            # templates active at the start
            for s in sites:
                for m in svc.mappings.active_mappings(s):
                    runtime["templates"].setdefault(m["template_id"], {
                        "status": "ok", "version": m["version"], "versions_used": [], "repairs_used": 0, "open_request": None,
                        "window": [], "record_count": 0, "unextracted_count": 0, "matched": 0, "filled_pages": 0,
                    })
            with svc.db.tx():
                svc.db.execute(
                    "INSERT INTO runs(run_id, idempotency_key, fingerprint, state, site, created_at, updated_at, finished_at, project_id, connection_id, platform_job_id, doc, runtime, parent_run_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, idem_key, fp, "queued", site, ts, ts, None, platform.get("project_id"), platform.get("connection_id"), platform.get("job_id"), dumps(doc), dumps(runtime),
                     (recollect_of or {}).get("run_id")),
                )
                if has_secret_part:
                    svc.secrets.put(run_id, "proxy_solver", secret_part(profile))
                svc.secrets.put(run_id, "webhook", profile["webhook"])
        self.wake()
        return 201, self.summary(run_id)

    # ================================================================== re-collect (README 4.10)
    async def recollect(self, parent_id: str, body: dict, idem_key: str) -> tuple[int, dict]:
        """A linked run retries failed documents and unfinished sitemap source cursors.

        The finished parent stays immutable; active mappings and a fresh profile apply.
        """
        row = self._row(parent_id)
        if row["state"] not in ("completed", "failed"):
            raise ApiError("run_not_finished", "only a finished run can be re-collected", details={"state": row["state"]})
        existing = self.svc.db.one("SELECT run_id FROM runs WHERE idempotency_key=?", (idem_key,))
        parent = loads(row["doc"])
        reasons = list(body.get("reasons") or DEFAULT_RECOLLECT_REASONS)
        if existing is not None:
            prior = loads(self._row(existing["run_id"])["doc"])
            intent = prior.get("recollect_of") or {}
            if (intent.get("run_id") != parent_id or intent.get("reasons") != reasons
                    or _canon_numbers(prior.get("platform")) != _canon_numbers(body["platform"])):
                raise ApiError("idempotency_key_reused", "the Idempotency-Key was used with another recovery request",
                               details={"run_id": existing["run_id"]}, headers={"Location": f"/v1/runs/{existing['run_id']}"})
            return 200, self.summary(existing["run_id"])
        marks = ",".join("?" for _ in reasons)
        urls: list[dict] = []
        seen: set[tuple[str, str | None]] = set()
        by_reason: dict[str, int] = {}
        recovery_entries: list[dict] = []
        parent_runtime = loads(row["runtime"]) or {}
        parent_mode = (parent.get("resolved") or {}).get("discovery_mode") or parent["config"]["discovery_mode"]

        def add_url(url, key, reason, source="list"):
            identity = (url, key or None)
            if identity in seen:
                return
            limit = self.svc.settings.limits["max_url_list_entries"]
            if len(urls) >= limit:
                raise validation_failed([field_error("urls", "limit_exceeded", f"more than {limit} recovery URLs", limit)])
            seen.add(identity)
            by_reason[reason] = by_reason.get(reason, 0) + 1
            entry = {"url": url}
            if key:
                entry["key"] = key
            urls.append(entry)
            recovery_entries.append({"url": url, "source": source if source in ("list", "link", "sitemap", "start") else "list",
                                     "source_key": key})

        for r in self.svc.db.all(f"SELECT url, reason, doc FROM unextracted WHERE run_id=? AND reason IN ({marks}) ORDER BY url, url_key",
                                 (parent_id, *reasons)):
            d = loads(r["doc"])
            url = d.get("requested_url") or r["url"]
            add_url(url, d.get("source_key"), r["reason"])
        recover_queue = row["state"] == "failed" and "stopped" in reasons
        if recover_queue:
            # Source cursors can already be committed while admitted work is
            # still queued. Read durable work in bounded batches, never replay
            # completed requests or mutate the failed parent.
            after = ""
            while True:
                batch = self.svc.db.all("SELECT unique_key,url,source,source_key FROM urls WHERE run_id=? AND status='queued' AND unique_key>? ORDER BY unique_key LIMIT 500",
                                        (parent_id, after))
                if not batch:
                    break
                for pending in batch:
                    add_url(pending["url"], pending["source_key"], "stopped", pending["source"])
                after = batch[-1]["unique_key"]
            after_seq = 0
            while True:
                batch = self.svc.db.all("SELECT b.seq,b.url,b.source,b.source_key FROM link_backlog b WHERE b.run_id=? AND b.seq>? "
                                        "AND (b.source='refetch' OR NOT EXISTS (SELECT 1 FROM urls u WHERE u.run_id=b.run_id AND u.status='done' AND u.url=b.url AND COALESCE(u.source_key,'')=COALESCE(b.source_key,''))) "
                                        "ORDER BY b.seq LIMIT 500", (parent_id, after_seq))
                if not batch:
                    break
                for pending in batch:
                    add_url(pending["url"], pending["source_key"], "stopped", pending["source"])
                after_seq = batch[-1]["seq"]
            # Stored pages still await extraction/repair. Admitted refetches
            # live here and in Crawlee, rather than in queued URL-table rows.
            after_page = ""
            while True:
                batch = self.svc.db.all("SELECT page_key,doc FROM stored_pages WHERE run_id=? AND page_key>? ORDER BY page_key LIMIT 500",
                                        (parent_id, after_page))
                if not batch:
                    break
                for pending in batch:
                    page = loads(pending["doc"])
                    add_url(page.get("requested_url") or page["url"], page.get("source_key"), "stopped")
                after_page = batch[-1]["page_key"]
            # Each input lane commits its cursor with admission. A recovery's
            # private seed is authoritative: config.urls merely mirrors it and
            # must not replay completed/refused work through a second lane.
            pending, cursor, key_name = [], "list_fed", "key"
            if "recollect_pending" in parent_runtime:
                pending = parent_runtime["recollect_pending"] or []
                cursor, key_name = "recollect_pending_fed", "source_key"
            elif not parent["config"].get("single_page"):
                if parent_mode == "url_list":
                    pending = dedupe_url_list(parent["config"].get("urls") or [])
                elif parent_mode in ("sitemap", "sitemap_and_crawl"):
                    pending = parent_runtime.get("sitemap_retry_urls") or []
                    cursor = "sitemap_retry_fed"
            fed = max(0, int(parent_runtime.get(cursor, 0)))
            for entry in pending[fed:]:
                add_url(entry["url"], entry.get(key_name), "stopped", entry.get("source", "list"))
        from app.jobs.discovery import sitemap_coverage, sitemap_retry_state

        traversal = parent_runtime.get("sitemap_traversal") or {}
        retry_sources = bool(traversal) and not sitemap_coverage(parent_runtime)["complete"]
        if not urls and not retry_sources and existing is None:
            counts = {r["reason"]: int(r["n"]) for r in self.svc.db.all("SELECT reason, COUNT(*) AS n FROM unextracted WHERE run_id=? GROUP BY reason", (parent_id,))}
            raise ApiError("nothing_to_recollect", "the run has no unextracted URLs with these reasons",
                           details={"reasons": reasons, "unextracted_by_reason": counts})
        config = copy.deepcopy(parent["config"])
        config["discovery_mode"] = "url_list"
        config["urls"] = urls
        config["rebuild_mapping"] = False
        config["sitemap_url"] = None
        follow_links = parent_mode in ("crawl", "sitemap_and_crawl") and not config.get("single_page")
        if recover_queue and follow_links:
            config.update(discovery_mode="crawl", urls=[])
        link = {"run_id": parent_id, "reasons": reasons, "url_count": len(urls), "by_reason": by_reason}
        retry = None
        if retry_sources:
            # Seed atomically in create(), before the manager can start the new run.
            config["discovery_mode"] = "sitemap_and_crawl" if recover_queue and follow_links else "sitemap"
            config["urls"] = []
            config["sitemap_url"] = next(iter(traversal["sources"]), None)
            link["source_count"] = sum(e.get("status") in ("pending", "failed") for e in traversal["sources"].values()) + int(bool(traversal.get("pending_root")))
            retry = {"traversal": sitemap_retry_state(traversal), "urls": [] if recover_queue else urls,
                     "roots": parent_runtime.get("sitemap_retry_roots") or parent_runtime.get("sitemaps") or list(traversal["sources"])}
        return await self.create({"config": config, "profile": body["profile"], "platform": body["platform"]}, idem_key,
                                 recollect_of=link, sitemap_retry=retry, queue_retry=recovery_entries if recover_queue else None)

    # ================================================================== reading
    def _row(self, run_id: str):
        row = self.svc.db.one("SELECT * FROM runs WHERE run_id=?", (run_id,))
        if row is None:
            raise ApiError("run_not_found", "unknown run_id")
        return row

    def summary(self, run_id: str) -> dict:
        row = self._row(run_id)
        runner = self.runners.get(run_id)
        doc = copy.deepcopy(runner.doc) if runner is not None and runner.doc.get("state") == row["state"] else loads(row["doc"])
        rt = copy.deepcopy(runner.rt) if runner is not None else loads(row["runtime"])
        doc["state"] = row["state"]
        doc.pop("queued_reason", None)  # recompute from the current state and capacity
        # the effective politeness and the hosts the automatic slow-down widened
        from app.core.config import politeness_text

        cfg = doc.get("config") or {}
        if cfg.get("delay_min_seconds") is not None:
            pol = {"preset": cfg.get("politeness") or "normal", "text": politeness_text(cfg)}
            slowed = {h: v for h, v in ((rt.get("slowdown") or {}).items()) if v.get("factor", 1) > 1}
            if slowed:
                pol["slowed_hosts"] = {h: {"factor": round(v["factor"], 2), "since": v.get("since"), "reason": v.get("reason", "")[:300]}
                                       for h, v in slowed.items()}
            doc["politeness"] = pol
        db = self.svc.db
        templates = []
        matched_total = filled_total = 0
        for tid, t in sorted((rt.get("templates") or {}).items()):
            version = t.get("version")
            if not version:
                active = self.svc.mappings.active(doc["site"], tid)
                version = int(active["version"]) if active else None
            if not version:
                continue
            status = t["status"]
            if doc["state"] in ("completed", "failed") and status == "repairing":
                status = "failed"
            unextracted = int(db.scalar("SELECT COUNT(*) FROM unextracted WHERE run_id=? AND template_id=?", (run_id, tid)) or 0)
            stored = int(db.scalar("SELECT COUNT(*) FROM stored_pages WHERE run_id=? AND template_id=?", (run_id, tid)) or 0)
            matched = t.get("matched", 0)
            filled = t.get("filled_pages", 0)
            matched_total += matched
            filled_total += filled
            entry = {
                "template_id": tid,
                "status": status,
                "mapping_version": version,
                "mapping_versions_used": sorted(t.get("versions_used") or []),
                "record_count": int(db.scalar("SELECT COUNT(*) FROM records WHERE run_id=? AND template_id=?", (run_id, tid)) or 0),
                "unextracted_count": unextracted,
                "stored_pages_pending": stored,
                "repairs_used": t.get("repairs_used", 0),
                "open_mapping_request_id": t.get("open_request"),
                "required_fill_rate": round(filled / matched, 4) if matched else None,
            }
            templates.append(entry)
        doc["templates"] = templates
        doc["counters"]["unextracted"] = int(db.scalar("SELECT COUNT(*) FROM unextracted WHERE run_id=?", (run_id,)) or 0)
        doc["counters"]["required_fill_rate"] = round(filled_total / matched_total, 4) if matched_total else None
        mrqs = db.all("SELECT doc FROM mapping_requests WHERE run_id=? ORDER BY created_at", (run_id,))
        doc["mapping_requests"] = [
            {k: d[k] for k in ("mapping_request_id", "reason", "status", "template_id", "created_at", "closed_at")}
            for d in (loads(r["doc"]) for r in mrqs)
        ]
        doc["progress"]["open_mapping_requests"] = sum(1 for m in doc["mapping_requests"] if m["status"] == "open")
        doc["events"] = self.svc.events.for_run(run_id)
        # completeness, the previous comparable run, a plain sentence
        from app.jobs.report import comparison, completeness, explanation

        if rt.get("sitemap_traversal"):
            from app.jobs.discovery import sitemap_coverage

            doc["sitemap_discovery"] = sitemap_coverage(rt)
            if rt.get("sitemap_probe"):
                doc["sitemap_probe"] = copy.deepcopy(rt["sitemap_probe"])
        cur = completeness(db, run_id, doc)
        doc["completeness"] = cur
        cmp = comparison(db, run_id, doc, cur, self.svc.settings.compare_drop_share)
        if cmp is not None:
            doc["comparison"] = cmp
        problems = []
        for e in doc["events"]:
            if e.get("event_type") == "problem":
                try:
                    problems.append(self.svc.events.payload(e["event_id"]))
                except Exception:
                    pass
        doc["explanation"] = explanation(doc, cur, [p for p in problems if p.get("message")])
        if cmp is not None and cmp.get("warning"):
            doc["explanation"]["sentence"] = (doc["explanation"]["sentence"] + " " + cmp["warning"])[:1000]
        doc.setdefault("timeline", [])
        secrets = doc["secrets"]
        if secrets.get("proxy_and_solver") == "kept" and self.svc.secrets.present(run_id, "proxy_solver") and not self.svc.secrets.readable(run_id, "proxy_solver"):
            secrets["proxy_and_solver"] = "missing"
        if self.svc.secrets.present(run_id, "webhook"):
            secrets["signing_secret"] = "kept" if self.svc.secrets.readable(run_id, "webhook") else "missing"
        if doc["state"] == "queued" and (reason := self.queued_reason(run_id, row["site"])) is not None:
            doc["queued_reason"] = reason
        children = db.all("SELECT run_id, state, created_at FROM runs WHERE parent_run_id=? ORDER BY created_at", (run_id,))
        if children:
            doc["recollected_by"] = [{"run_id": c["run_id"], "state": c["state"], "created_at": c["created_at"]} for c in children]
        if doc["state"] == "completed" and doc.get("result"):
            doc["result"] = self.result_descriptor(run_id, doc)
        if doc.get("finished_at"):
            doc["expires_at"] = iso(plus(self.svc.settings.job_record_days * 86400, parse(doc["finished_at"])))
        return doc

    def result_descriptor(self, run_id: str, doc: dict) -> dict:
        res = dict(doc["result"])
        return res

    def list_item(self, row) -> dict:
        doc = loads(row["doc"])
        runner = self.runners.get(row["run_id"])
        if runner is not None:
            doc = runner.doc
        item = {
            "run_id": row["run_id"],
            "state": row["state"],
            "outcome": doc.get("outcome"),
            "stop_reason": doc.get("stop_reason"),
            "site": doc["site"],
            "created_at": doc["created_at"],
            "finished_at": doc.get("finished_at"),
            "platform": doc["platform"],
            "pages_fetched": doc["counters"]["pages_fetched"],
            "records_extracted": doc["counters"]["records_extracted"],
            "required_fill_rate": doc["counters"].get("required_fill_rate"),
            "events_pending": self.svc.events.pending_count(row["run_id"]),
        }
        if doc.get("profile", {}).get("profile_name"):
            item["profile_name"] = doc["profile"]["profile_name"]
        # what a runs table needs in one call (README 4.9): found / collected / missed, "M of N"
        keys = ("target_pages_found", "pages_collected", "records", "not_collected", "not_collected_by_reason")
        snap = doc.get("completeness") if row["state"] in ("completed", "failed") else None
        if not snap or any(k not in snap for k in keys):
            from app.jobs.report import completeness

            snap = completeness(self.svc.db, row["run_id"], dict(doc, state=row["state"]))
        comp = {k: snap[k] for k in keys}
        if snap.get("sitemap_discovery"):
            comp["sitemap_discovery"] = snap["sitemap_discovery"]
        if snap.get("sitemap_probe"):
            comp["sitemap_probe"] = snap["sitemap_probe"]
        if row["state"] not in ("completed", "failed") and snap.get("progress"):
            comp["progress"] = snap["progress"]
        item["completeness"] = comp
        if row["state"] == "queued" and (reason := self.queued_reason(row["run_id"], row["site"])) is not None:
            item["queued_reason"] = reason
        return item

    # ================================================================== control
    async def stop(self, run_id: str) -> dict:
        row = self._row(run_id)
        state = row["state"]
        if state == "queued":
            doc = loads(row["doc"])
            rt = loads(row["runtime"])
            rt["stop_reason"] = "stopped"
            self._list_unfetched_urls(run_id, doc)
            await self._complete_doc(run_id, doc, rt, None)
        elif state in ("running", "waiting_for_credentials"):
            self._set_state(run_id, "stopping")
            runner = self.runners.get(run_id)
            if runner is not None:
                runner.doc["state"] = "stopping"
            else:
                self._start_runner(run_id)
        return self.summary(run_id)

    def _list_unfetched_urls(self, run_id: str, doc: dict) -> None:
        """A queued url_list job stopped before it ran: every URL of the list is listed
        ``stopped`` (README 4.5 "any URL from urls")."""
        from app.core.config import dedupe_url_list
        from app.jobs.runner import write_unextracted

        cfg = doc["config"]
        if cfg.get("discovery_mode") != "url_list" or not cfg.get("urls"):
            return
        with self.svc.db.tx():
            for entry in dedupe_url_list(cfg["urls"]):
                try:
                    canonical = canonical_url(entry["url"])
                    site = site_key(entry["url"])
                except ValueError:
                    continue
                write_unextracted(self.svc.db, run_id, canonical, entry["url"], entry.get("key"), site, "stopped", None, None, 0,
                                  "not fetched before the job stopped")

    async def resume(self, run_id: str, profile: dict) -> dict:
        row = self._row(run_id)
        if not self.svc.secrets.configured:
            raise ApiError("secrets_key_unavailable", "the service has no job-secret encryption key configured", headers={"Retry-After": "300"})
        state = row["state"]
        active = self.runners.get(run_id)
        owns_state = active is not None and active.state == state
        # A paused runner may still be draining its queue. Preserve its latest
        # budgets and synchronize replacement metadata before it persists again.
        doc = copy.deepcopy(active.doc) if owns_state else loads(row["doc"])
        rt = copy.deepcopy(active.rt) if owns_state else loads(row["runtime"])
        replace_credentials = state not in ("completed", "failed") and bool(profile.get("proxy_tiers") or profile.get("solver"))
        with self.svc.db.tx():
            if replace_credentials:
                self.svc.secrets.put(run_id, "proxy_solver", secret_part(profile))
                summary = profile_summary(profile)
                doc["profile"].update({key: summary[key] for key in ("proxy_tiers", "solver")})
                if "profile_name" in summary:
                    doc["profile"]["profile_name"] = summary["profile_name"]
                rt["needs_secrets"] = bool(doc["config"].get("use_solver")) or needs_proxy_or_solver_secrets(doc["config"], profile)
                if doc["secrets"].get("proxy_and_solver") in ("not_configured", "missing"):
                    doc["secrets"]["proxy_and_solver"] = "kept"
            if profile.get("webhook"):
                self.svc.secrets.put(run_id, "webhook", profile["webhook"])
                doc["secrets"]["signing_secret"] = "kept"
                doc["profile"]["webhook_url"] = profile["webhook"]["url"]
            self.svc.db.execute("UPDATE runs SET doc=?, runtime=? WHERE run_id=?", (dumps(doc), dumps(rt), run_id))
        if owns_state:
            active.doc["profile"] = copy.deepcopy(doc["profile"])
            active.doc["secrets"] = copy.deepcopy(doc["secrets"])
            if replace_credentials:
                active.rt["needs_secrets"] = rt["needs_secrets"]
        if state == "waiting_for_credentials" and self._covers(run_id, doc["config"], rt):
            # The paused crawler must finish reclaiming in-flight requests before
            # a new runner opens the same persistent queue.
            old_runner = self.runners.get(run_id)
            if old_runner is not None and old_runner.task is not None and not old_runner.task.done():
                await asyncio.shield(old_runner.task)
            fresh = self._row(run_id)
            if fresh["state"] == "waiting_for_credentials":
                rt = loads(fresh["runtime"])
                # Another Resume can replace the profile while we wait for the
                # old owner. Validate the latest committed replacement again.
                if self._covers(run_id, loads(fresh["doc"])["config"], rt):
                    rt.pop("credentials_wait_reason", None)
                    self.svc.db.execute("UPDATE runs SET runtime=? WHERE run_id=?", (dumps(rt), run_id))
                    self._set_state(run_id, "running")
                    self._start_runner(run_id)
        elif state in ("running", "stopping") and (profile.get("proxy_tiers") or profile.get("solver")):
            # rotate a running job's secrets in memory too (README 15.26)
            runner = self.runners.get(run_id)
            if runner is not None and runner.tiers is not None:
                secret = runner._load_secret()
                if secret is not None:
                    runner.apply_secret(secret)
        self.svc.events.wake_pending_for_run(run_id)
        return self.summary(run_id)

    def _covers(self, run_id: str, config: dict, rt: dict) -> bool:
        """The approved profile covers the job and decrypts when credentials are needed."""
        from app.core.secrets import SecretsUnavailable

        if rt.get("needs_secrets"):
            try:
                secret = self.svc.secrets.get(run_id, "proxy_solver")
            except SecretsUnavailable:
                return False
        else:
            # IP allow-list profiles need no decrypted credentials, but must
            # still cover the job's selected tiers after a replacement.
            secret = loads(self._row(run_id)["doc"])["profile"]
        wanted = config.get("use_proxy_tiers") or []
        have = {t["tier"] for t in secret.get("proxy_tiers") or []}
        if wanted and not set(wanted) <= have:
            return False
        if config.get("use_solver") and not secret.get("solver"):
            return False
        for tier in selected_tiers(config, secret):
            if tier.get("provider") == "custom_list" and not tier.get("proxy_urls"):
                return False
            if tier.get("provider") != "custom_list" and tier.get("auth_mode") == "user_password" and not (tier.get("username") and tier.get("password")):
                return False
        return True

    async def to_waiting_for_credentials(self, run_id: str, *, reason: str = "secrets_unavailable") -> None:
        self._set_state(run_id, "waiting_for_credentials")
        row = self.svc.db.one("SELECT doc, runtime FROM runs WHERE run_id=?", (run_id,))
        doc = loads(row["doc"])
        if reason == "secrets_unavailable":
            doc["secrets"]["proxy_and_solver"] = "missing"
        self.svc.db.execute("UPDATE runs SET doc=? WHERE run_id=?", (dumps(doc), run_id))
        from app.fetching.proxy_auth import PROXY_AUTH_MESSAGE

        if reason == "proxy_auth_failed":
            code, message = "proxy_auth_failed", PROXY_AUTH_MESSAGE
            problem_key = f"{code}:{loads(row['runtime']).get('proxy_auth_pauses', 0)}"
        else:
            code = "waiting_for_credentials"
            message = "the job's proxy or solver secrets cannot be decrypted; send them again with Resume"
            problem_key = f"{code}:"
        self.svc.events.create(
            run_id, "problem", run_id,
            {"problem_code": code, "message": message},
            problem_key=problem_key,
        )

    # ================================================================== completion
    async def complete_run(self, runner: "JobRunner") -> None:
        runner._accumulate_running_time()
        stop_reason = runner.rt.get("stop_reason") or "finished"
        runner.rt["stop_reason"] = stop_reason
        runner.leftovers_for_result(stop_reason)
        runner.doc["progress"]["result_status"] = "writing"
        runner._persist()
        await self._complete_doc(runner.run_id, runner.doc, runner.rt, runner)

    async def _complete_doc(self, run_id: str, doc: dict, rt: dict, runner) -> None:
        svc = self.svc
        stop_reason = rt.get("stop_reason") or "finished"
        site = doc["site"]
        results_dir = Path(svc.settings.data_dir) / "results"
        results_dir.mkdir(parents=True, exist_ok=True)
        name = result_file_name(site, run_id)
        path = results_dir / name
        tmp = path.with_suffix(".tmp")
        sha = hashlib.sha256()
        size = 0
        count = 0
        per_template: dict[str, int] = {}
        with open(tmp, "wb") as fh:
            for row in svc.db.all("SELECT record_id, template_id, line FROM records WHERE run_id=? ORDER BY record_id", (run_id,)):
                data = row["line"].encode("utf-8") + b"\n"
                fh.write(data)
                sha.update(data)
                size += len(data)
                count += 1
                per_template[row["template_id"]] = per_template.get(row["template_id"], 0) + 1
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        names = run_file_names(site, run_id)
        csv_info = self._write_result_csv(run_id, results_dir / names["result_csv"])
        ts = now()
        if rt.get("sitemap_traversal"):
            from app.jobs.discovery import sitemap_coverage

            doc["sitemap_discovery"] = sitemap_coverage(rt)
            if rt.get("sitemap_probe"):
                doc["sitemap_probe"] = copy.deepcopy(rt["sitemap_probe"])
            rt["sitemap_partial"] = not doc["sitemap_discovery"]["complete"] or bool(rt.get("sitemap_probe_partial"))
        templates_failed = any(t.get("status") == "failed" for t in (rt.get("templates") or {}).values())
        # outcome-neutral entries: not_found, and sitemap URLs refused by robots.txt or the private rule
        non_neutral = int(svc.db.scalar("SELECT COUNT(*) FROM unextracted WHERE run_id=? AND neutral=0", (run_id,)) or 0)
        outcome = "complete" if (stop_reason in ("finished", "max_records", "max_pages") and not templates_failed and non_neutral == 0 and not rt.get("navigation_partial") and not rt.get("sitemap_partial") and not rt.get("discovery_partial")) else "partial"
        result = {
            "run_id": run_id,
            "format": "jsonl",
            "file_name": name,
            "size_bytes": size,
            "record_count": count,
            "sha256": sha.hexdigest(),
            "templates": [{"template_id": k, "record_count": v} for k, v in sorted(per_template.items())],
            "outcome": outcome,
            "stop_reason": stop_reason,
            "created_at": iso(ts),
            "expires_at": iso(plus(svc.settings.result_days * 86400, ts)),
            "download_path": f"/v1/runs/{run_id}/result/download",
            "csv": dict(csv_info, download_path=f"/v1/runs/{run_id}/result/download?format=csv"),
        }
        doc["state"] = "completed"
        doc["finished_at"] = iso(ts)
        doc["outcome"] = outcome
        doc["stop_reason"] = stop_reason
        doc["result"] = result
        doc["progress"]["result_status"] = "written"
        doc["progress"]["queue_pending"] = 0
        doc["progress"]["in_progress"] = 0
        doc["progress"]["pending_reextraction"] = 0
        if doc["progress"]["discovery"] not in ("finished", "cut"):
            doc["progress"]["discovery"] = "cut" if stop_reason != "finished" else "finished"
        doc["counters"]["result_bytes"] = size
        try:  # the numbers a later run of the site compares with
            from app.jobs.report import completeness

            snap = completeness(svc.db, run_id, dict(doc, state="completed"))
            doc["completeness"] = {k: snap[k] for k in ("target_pages_found", "pages_collected", "records", "not_collected", "not_collected_by_reason")}
            if snap.get("sitemap_discovery"):
                doc["completeness"]["sitemap_discovery"] = snap["sitemap_discovery"]
            if snap.get("sitemap_probe"):
                doc["completeness"]["sitemap_probe"] = snap["sitemap_probe"]
        except Exception:
            pass
        tl = doc.setdefault("timeline", [])
        tl.append({"at": iso(ts), "kind": "finished", "message": f"finished: {outcome}, {stop_reason.replace('_', ' ')}, {count} records"})
        if len(tl) > 50:
            del tl[10:len(tl) - 40]
        if doc["secrets"].get("proxy_and_solver") in ("kept", "missing"):
            doc["secrets"]["proxy_and_solver"] = "discarded"
        for t in (rt.get("templates") or {}).values():
            if t.get("status") == "repairing":
                t["status"] = "failed"
        event_extra = {"outcome": outcome, "stop_reason": stop_reason}
        if doc.get("recollect_of"):
            event_extra["parent_run_id"] = doc["recollect_of"]["run_id"]
        with svc.db.tx():
            svc.db.execute(
                "UPDATE runs SET state='completed', updated_at=?, finished_at=?, doc=?, runtime=? WHERE run_id=?",
                (iso(ts), iso(ts), dumps(doc), dumps(rt), run_id),
            )
            svc.secrets.delete(run_id, "proxy_solver")
            svc.events.create(run_id, "run_completed", run_id, event_extra)
        if runner is not None:
            runner.doc = doc
            runner.cleanup_crawlee_storage()
        log.info("job %s completed: %s/%s, %d records", run_id, outcome, stop_reason, count)

    def _write_result_csv(self, run_id: str, path: Path) -> dict:
        """The result as CSV (README 7.5): same records, same order as the JSON Lines file."""
        from app.core.csvout import CsvFile, record_columns, record_row

        sql = "SELECT line FROM records WHERE run_id=? ORDER BY record_id"
        columns = record_columns(json.loads(r["line"]) for r in self.svc.db.all(sql, (run_id,)))
        out = CsvFile(path)
        try:
            out.write(columns)
            for r in self.svc.db.all(sql, (run_id,)):
                out.write(record_row(json.loads(r["line"]), columns))
            return out.close()
        except BaseException:
            out.abort()
            raise

    async def fail_run(self, runner: "JobRunner", code: str, message: str) -> None:
        runner._accumulate_running_time()
        await self._fail_doc(runner.run_id, runner.doc, runner.rt, code, message)
        runner.doc["state"] = "failed"

    async def _fail_doc(self, run_id: str, doc: dict, rt: dict, code: str, message: str) -> None:
        svc = self.svc
        ts = now_iso()
        doc["state"] = "failed"
        doc["finished_at"] = ts
        doc["outcome"] = None
        doc["stop_reason"] = None
        doc["error"] = {"code": code, "message": message[:2000], "at": ts}
        doc["progress"]["result_status"] = "not_available"
        if doc["secrets"].get("proxy_and_solver") in ("kept", "missing"):
            doc["secrets"]["proxy_and_solver"] = "discarded"
        with svc.db.tx():
            svc.db.execute("UPDATE runs SET state='failed', updated_at=?, finished_at=?, doc=?, runtime=? WHERE run_id=?", (ts, ts, dumps(doc), dumps(rt), run_id))
            svc.secrets.delete(run_id, "proxy_solver")
            svc.db.execute(
                "UPDATE mapping_requests SET status='cancelled', doc=json_set(doc,'$.status','cancelled','$.closed_at',?) WHERE run_id=? AND status='open'",
                (ts, run_id),
            )
            svc.events.create(run_id, "run_failed", run_id, {"message": message[:500]})
        log.warning("job %s failed: %s", run_id, code)

    # ================================================================== activation hook
    def notify_activation(self, site: str, template_id: str, version: int, mapping: dict, resolved_ids: list[str], new_template: bool) -> None:
        for runner in list(self.runners.values()):
            if site in (runner.doc.get("sites") or [runner.doc["site"]]):
                runner.on_activation(site, template_id, version, mapping, resolved_ids, new_template)

    def notify_request_closed(self, run_id: str, mrq_id: str, status: str) -> bool:
        runner = self.runners.get(run_id)
        if runner is not None:
            runner.on_request_closed(mrq_id, status)
            return True
        return False

    # ================================================================== retention
    async def _retention(self) -> None:
        """The automatic sweeper (README "Disk use and clean-up"). Skips a pass while a
        ``POST /v1/admin/cleanup`` holds the housekeeping lock; records a summary of each pass."""
        settings = self.svc.settings
        ts = now()
        last = getattr(self, "_last_retention", None)
        if last is not None and (ts - last).total_seconds() < settings.retention_interval_seconds:
            return
        if self.housekeeping_lock.locked():
            return
        async with self.housekeeping_lock:
            self._last_retention = ts
            self.retention_pass(ts)

    def retention_pass(self, ts) -> dict:
        import shutil

        from app.jobs import housekeeping as hk

        settings = self.svc.settings
        db = self.svc.db
        summary = hk.new_summary("automatic")
        summary["at"] = iso(ts)
        data = Path(settings.data_dir)
        for row in db.all("SELECT run_id, finished_at, doc FROM runs WHERE state IN ('completed','failed') AND finished_at IS NOT NULL"):
            run_id = row["run_id"]
            if run_id in self.runners:
                continue
            finished = parse(row["finished_at"])
            age = (ts - finished).total_seconds()
            if age > settings.stored_pages_days * 86400:
                job_dir = data / "jobs" / run_id
                if job_dir.exists():
                    size = hk.tree_bytes(job_dir)
                    shutil.rmtree(job_dir, ignore_errors=True)
                    for table in hk.WORKING_TABLES:
                        db.execute(f"DELETE FROM {table} WHERE run_id=?", (run_id,))
                    hk.add(summary, "run_working_files", 1, size)
            doc = loads(row["doc"])
            if doc.get("result") and parse(doc["result"]["expires_at"]) <= ts:
                # the result expires in both formats
                for name in {doc["result"]["file_name"], *run_file_names(doc["site"], run_id).values()}:
                    path = data / "results" / name
                    if path.exists():
                        size = hk.tree_bytes(path)
                        path.unlink(missing_ok=True)
                        hk.add(summary, "result_files", 1, size)
            if age > settings.job_record_days * 86400:
                with db.tx():
                    for table in hk.RUN_TABLES:
                        db.execute(f"DELETE FROM {table} WHERE run_id=?", (run_id,))
                    db.execute("DELETE FROM runs WHERE run_id=?", (run_id,))
                hk.add(summary, "run_records", 1, 0)
        removed = self.svc.samples.expire()
        for kind, (items, size) in removed.items():
            hk.add(summary, kind, items, size)
        hk.save_summary(self.svc, summary)
        return summary
