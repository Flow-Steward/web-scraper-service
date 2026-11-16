"""Webhook delivery with the contract's back-off (README 8.5, 9).

* The schedule is a list of offsets from the generation's start; the next attempt is
  planned from the *previous attempt* by the gap between two offsets, so an outage of the
  service never turns the remaining offsets into a burst of back-to-back attempts.
* At start every pending event is due at once (README 8.5 "continue at once"), including
  events parked with ``signing_secret_unavailable`` when the key is right again.
* Webhook URLs are not crawl targets: private, loopback and plain-http addresses are
  allowed (``WSS_WEBHOOK_ALLOW_PRIVATE``, default on); redirects are never followed, so the
  signature headers only ever reach the configured host.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import TYPE_CHECKING

import httpx

from app.core.db import dumps, loads
from app.core.secrets import SecretsUnavailable
from app.core.timeutil import iso, now, now_iso, parse, plus
from app.events.journal import sign, sign_body
from app.settings import SERVICE_VERSION

if TYPE_CHECKING:
    from app.service import Service

log = logging.getLogger("wss.delivery")


class DeliveryWorker:
    def __init__(self, svc: "Service") -> None:
        self.svc = svc
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._inflight: set[str] = set()
        self._tasks: set[asyncio.Task] = set()  # strong references
        self._stopping = False

    def wake(self) -> None:
        self._wake.set()

    def revive_pending(self) -> int:
        """Every pending event is due now; parked ones whose signing secret decrypts again revive."""
        ts = now_iso()
        n = self.svc.db.execute("UPDATE events SET next_attempt_at=? WHERE state='pending' AND next_attempt_at IS NOT NULL", (ts,))
        parked = self.svc.db.all(
            "SELECT event_id, run_id FROM events WHERE state='pending' AND next_attempt_at IS NULL AND json_extract(doc,'$.last_error')='signing_secret_unavailable'"
        )
        for row in parked:
            if self.svc.secrets.readable(row["run_id"], "webhook"):
                self.svc.db.execute("UPDATE events SET next_attempt_at=? WHERE event_id=?", (ts, row["event_id"]))
                n += 1
        return n

    async def start(self) -> None:
        self.revive_pending()
        self._task = asyncio.create_task(self._loop(), name="webhook-delivery")

    async def shutdown(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                due = self.svc.db.all(
                    "SELECT event_id FROM events WHERE state='pending' AND next_attempt_at IS NOT NULL AND next_attempt_at <= ? ORDER BY next_attempt_at LIMIT 50",
                    (now_iso(),),
                )
                for row in due:
                    if row["event_id"] not in self._inflight:
                        self._inflight.add(row["event_id"])
                        task = asyncio.create_task(self._attempt(row["event_id"]))
                        self._tasks.add(task)
                        task.add_done_callback(self._tasks.discard)
            except Exception:
                log.exception("delivery loop step failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    def _offset(self, n: int) -> float:
        """Offset of attempt ``n`` (0-based) from the generation start, without jitter."""
        schedule = self.svc.settings.webhook_schedule_seconds
        if n < len(schedule):
            return float(schedule[n])
        return float(schedule[-1] if schedule else 0) + (n - len(schedule) + 1) * self.svc.settings.webhook_repeat_seconds

    def _next_gap(self, attempts_done: int) -> float:
        """Seconds from the attempt just made to the next one: the gap between two scheduled
        offsets (with jitter), so the schedule keeps its spacing after any delay."""
        gap = self._offset(attempts_done) - self._offset(attempts_done - 1) if attempts_done > 0 else self._offset(0)
        jitter = self.svc.settings.webhook_jitter
        return max(0.0, gap * (1 + random.uniform(-jitter, jitter)))

    def _next_offset(self, attempts_done: int) -> float:  # kept for the schedule unit test
        jitter = self.svc.settings.webhook_jitter
        return max(0.0, self._offset(attempts_done) * (1 + random.uniform(-jitter, jitter)))

    async def _attempt(self, event_id: str) -> None:
        svc = self.svc
        try:
            row = svc.db.one("SELECT * FROM events WHERE event_id=?", (event_id,))
            if row is None or row["state"] != "pending":
                return
            doc = loads(row["doc"])
            generation = doc["generation"]
            run_id = row["run_id"]
            try:
                webhook = svc.secrets.get(run_id, "webhook")
            except SecretsUnavailable:
                doc["last_error"] = "signing_secret_unavailable"
                svc.db.execute(
                    "UPDATE events SET next_attempt_at=NULL, doc=? WHERE event_id=? AND state='pending'", (dumps(doc), event_id)
                )
                return
            body = doc["body"].encode("utf-8")
            ts = str(int(time.time()))
            if not svc.settings.webhook_allow_private:
                from app.core.netguard import url_is_private

                if await url_is_private(webhook["url"]):
                    doc["last_error"] = "webhook_private_address_refused"
                    svc.db.execute("UPDATE events SET next_attempt_at=NULL, doc=? WHERE event_id=? AND state='pending'", (dumps(doc), event_id))
                    return
            headers = {
                "Content-Type": "application/json",
                "X-Scraper-Signature": sign(webhook["signing_secret"], ts, body),
                "X-Scraper-Body-Signature": sign_body(webhook["signing_secret"], body),
                "X-Scraper-Timestamp": ts,
                "Idempotency-Key": f"{event_id}.{generation}",
                "User-Agent": f"web-scraper-service/{SERVICE_VERSION}",
            }
            status = None
            error = None
            try:
                async with httpx.AsyncClient(timeout=svc.settings.webhook_timeout_seconds, follow_redirects=False) as client:
                    resp = await client.post(webhook["url"], content=body, headers=headers)
                    status = resp.status_code
                    if not 200 <= status < 300:
                        error = f"HTTP {status}"
            except (httpx.HTTPError, OSError) as exc:
                error = type(exc).__name__
            with svc.db.tx():
                row = svc.db.one("SELECT * FROM events WHERE event_id=?", (event_id,))
                fresh = loads(row["doc"])
                if fresh["generation"] != generation:
                    return  # a resend started a new generation meanwhile
                fresh["attempts"] = fresh.get("attempts", 0) + 1
                fresh["last_attempt_at"] = now_iso()
                fresh["last_status_code"] = status
                fresh["last_error"] = error
                if error is None:
                    if fresh.get("delivered_at") is None:
                        fresh["delivered_at"] = now_iso()
                    new_state = "delivered" if row["state"] == "pending" else row["state"]
                    svc.db.execute("UPDATE events SET state=?, next_attempt_at=NULL, doc=? WHERE event_id=?", (new_state, dumps(fresh), event_id))
                    log.info("event %s generation %d delivered", event_id, generation)
                    return
                if row["state"] != "pending":
                    svc.db.execute("UPDATE events SET doc=? WHERE event_id=?", (dumps(fresh), event_id))
                    return
                next_at = plus(self._next_gap(fresh["attempts"]), now())
                # stop retrying once the job record's retention ends
                run = svc.db.one("SELECT finished_at FROM runs WHERE run_id=?", (row["run_id"],))
                if run is not None and run["finished_at"]:
                    end = plus(svc.settings.job_record_days * 86400, parse(run["finished_at"]))
                    if next_at > end:
                        next_at = None
                svc.db.execute(
                    "UPDATE events SET next_attempt_at=?, doc=? WHERE event_id=?",
                    (iso(next_at) if next_at else None, dumps(fresh), event_id),
                )
                log.info("event %s attempt %d failed (%s); next %s", event_id, fresh["attempts"], error, iso(next_at) if next_at else "never")
        except Exception:
            log.exception("delivery of %s failed unexpectedly", event_id)
        finally:
            self._inflight.discard(event_id)
