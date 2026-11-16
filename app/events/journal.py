"""Event journal, claims, stages and resend (README 8)."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Callable

from app.core.db import Database, dumps, loads
from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.timeutil import iso, now, now_iso, parse, plus


def sign(secret: str, timestamp: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), timestamp.encode("ascii") + b"." + body, hashlib.sha256).hexdigest()


def sign_body(secret: str, body: bytes) -> str:
    """``X-Scraper-Body-Signature`` (README 9): HMAC-SHA256 of the raw body only, for the
    platform's older signed-webhook mode. Sent next to the timestamped signature."""
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def body_bytes(event: dict) -> bytes:
    return json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class EventJournal:
    def __init__(self, db: Database, claim_timeout_seconds: int, on_new: Callable[[], None] | None = None) -> None:
        self.db = db
        self.claim_timeout = claim_timeout_seconds
        self.on_new = on_new or (lambda: None)

    # ---------------------------------------------------------------- creation
    def create(
        self,
        run_id: str,
        event_type: str,
        resource_id: str,
        extra: dict[str, Any] | None = None,
        problem_key: str | None = None,
    ) -> str | None:
        """Creates an event (generation 1). Returns None when the problem key was already used."""
        if problem_key is not None:
            if self.db.one("SELECT 1 FROM events WHERE run_id=? AND problem_key=?", (run_id, problem_key)):
                return None
        event_id = new_id("evt")
        ts = now_iso()
        payload = {
            "event_id": event_id,
            "event_type": event_type,
            "run_id": run_id,
            "resource_id": resource_id,
            "occurred_at": ts,
        }
        for key, value in (extra or {}).items():
            if value is not None:
                payload[key] = value
        doc = {
            "payload": payload,
            "generation": 1,
            "gen_started_at": ts,
            "body": None,
            "resend_mode": None,
            "attempts": 0,
            "last_attempt_at": None,
            "last_status_code": None,
            "last_error": None,
            "delivered_at": None,
            "claim_id": None,
            "claim": None,
            "stages": [],
            "handled_at": None,
        }
        doc["body"] = body_bytes(self._body(doc)).decode("utf-8")
        self.db.execute(
            "INSERT INTO events(event_id, run_id, event_type, state, created_at, problem_key, next_attempt_at, doc) VALUES (?,?,?,?,?,?,?,?)",
            (event_id, run_id, event_type, "pending", ts, problem_key, ts, dumps(doc)),
        )
        self.on_new()
        return event_id

    @staticmethod
    def _body(doc: dict) -> dict:
        body = dict(doc["payload"])
        # field order of the contract's example: delivery_generation after occurred_at
        ordered = {k: body[k] for k in ("event_id", "event_type", "run_id", "resource_id", "occurred_at") if k in body}
        ordered["delivery_generation"] = doc["generation"]
        for key, value in body.items():
            if key not in ordered:
                ordered[key] = value
        return ordered

    # ---------------------------------------------------------------- reading
    def _row(self, event_id: str):
        row = self.db.one("SELECT * FROM events WHERE event_id=?", (event_id,))
        if row is None:
            raise ApiError("event_not_found", "unknown event_id")
        return row

    def _state(self, row, doc: dict) -> str:
        state = row["state"]
        if state == "claimed" and doc.get("claim") and parse(doc["claim"]["expires_at"]) <= now():
            state = "stale"
            self.db.execute("UPDATE events SET state='stale' WHERE event_id=? AND state='claimed'", (row["event_id"],))
        return state

    def record(self, row, doc: dict | None = None) -> dict:
        doc = doc if doc is not None else loads(row["doc"])
        state = self._state(row, doc)
        stages = doc.get("stages") or []
        return {
            "event_id": row["event_id"],
            "event_type": row["event_type"],
            "run_id": row["run_id"],
            "resource_id": doc["payload"]["resource_id"],
            "state": state,
            "created_at": row["created_at"],
            "delivery": {
                "generation": doc["generation"],
                "resend_mode": doc.get("resend_mode"),
                "attempts": doc.get("attempts", 0),
                "last_attempt_at": doc.get("last_attempt_at"),
                "last_status_code": doc.get("last_status_code"),
                "last_error": doc.get("last_error"),
                "delivered_at": doc.get("delivered_at"),
                "next_attempt_at": row["next_attempt_at"] if state == "pending" else None,
            },
            "claim": doc.get("claim"),
            "progress": {"stages": stages, "last_stage": stages[-1]["stage"] if stages else None},
            "handled_at": doc.get("handled_at"),
        }

    def for_run(self, run_id: str) -> list[dict]:
        rows = self.db.all("SELECT * FROM events WHERE run_id=? ORDER BY created_at, event_id", (run_id,))
        return [self.record(r) for r in rows]

    def pending_count(self, run_id: str) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM events WHERE run_id=? AND state='pending'", (run_id,)) or 0)

    def payload(self, event_id: str) -> dict:
        row = self._row(event_id)
        doc = loads(row["doc"])
        return json.loads(doc["body"])

    # ---------------------------------------------------------------- claims
    def claim(self, event_id: str, claimant: dict) -> dict:
        with self.db.tx():
            row = self._row(event_id)
            doc = loads(row["doc"])
            state = self._state(row, doc)
            ts = now()
            current = doc.get("claim")
            if state == "handled":
                raise ApiError("event_already_handled", "the event is handled")
            if state in ("claimed", "stale") and current:
                if current["claimant"]["job_id"] != claimant["job_id"]:
                    if state == "claimed":
                        raise ApiError(
                            "event_already_claimed",
                            "another platform job holds the event",
                            details={"claimant": current["claimant"], "expires_at": current["expires_at"]},
                        )
                    raise ApiError("event_stale", "the claim timed out; only its holder may continue, others need a resend")
                current["expires_at"] = iso(plus(self.claim_timeout, ts))
                current["claimant"] = claimant
                new_state = "claimed"
            else:
                doc["claim_id"] = new_id("clm")
                doc["claim"] = {"claimant": claimant, "claimed_at": iso(ts), "expires_at": iso(plus(self.claim_timeout, ts))}
                new_state = "claimed"
                if doc.get("delivered_at") is None and state == "pending":
                    # a claim also marks a pending event delivered (the 2xx may have been lost)
                    doc["delivered_at"] = iso(ts)
            self.db.execute(
                "UPDATE events SET state=?, next_attempt_at=NULL, doc=? WHERE event_id=?",
                (new_state, dumps(doc), event_id),
            )
            stages = doc.get("stages") or []
            return {
                "event": json.loads(doc["body"]),
                "state": new_state,
                "claim_id": doc["claim_id"],
                "claimed_at": doc["claim"]["claimed_at"],
                "expires_at": doc["claim"]["expires_at"],
                "delivery_generation": doc["generation"],
                "resend_mode": doc.get("resend_mode"),
                "progress": {"stages": stages, "last_stage": stages[-1]["stage"] if stages else None},
            }

    def _check_claim(self, doc: dict, claim_id: str) -> None:
        if not doc.get("claim_id") or not hmac.compare_digest(doc["claim_id"], claim_id):
            raise ApiError("claim_superseded", "the claim_id is not the event's current claim")

    def stage(self, event_id: str, claim_id: str, stage: str) -> dict:
        with self.db.tx():
            row = self._row(event_id)
            doc = loads(row["doc"])
            state = self._state(row, doc)
            self._check_claim(doc, claim_id)
            if state == "handled":
                raise ApiError("event_already_handled", "the event is handled")
            stages = doc.setdefault("stages", [])
            recorded = {s["stage"] for s in stages}
            if stage in recorded:
                return self.record(self._row(event_id))
            if stage == "completed" and "processed" not in recorded:
                raise ApiError("stage_out_of_order", "completed cannot be recorded before processed")
            ts = now()
            stages.append({"stage": stage, "recorded_at": iso(ts), "claimant_job_id": doc["claim"]["claimant"]["job_id"]})
            doc["claim"]["expires_at"] = iso(plus(self.claim_timeout, ts))
            self.db.execute("UPDATE events SET state='claimed', doc=? WHERE event_id=?", (dumps(doc), event_id))
            return self.record(self._row(event_id))

    def complete(self, event_id: str, claim_id: str) -> dict:
        with self.db.tx():
            row = self._row(event_id)
            doc = loads(row["doc"])
            self._check_claim(doc, claim_id)
            state = self._state(row, doc)
            if state != "handled":
                doc["handled_at"] = now_iso()
                self.db.execute("UPDATE events SET state='handled', next_attempt_at=NULL, doc=? WHERE event_id=?", (dumps(doc), event_id))
            return self.record(self._row(event_id))

    def resend(self, event_id: str, mode: str, revoke_live: bool, expected_generation: int | None) -> dict:
        with self.db.tx():
            row = self._row(event_id)
            doc = loads(row["doc"])
            state = self._state(row, doc)
            if expected_generation is not None and expected_generation != doc["generation"]:
                raise ApiError(
                    "event_generation_conflict",
                    "expected_generation is not the current generation",
                    details={"delivery_generation": doc["generation"]},
                )
            if state == "claimed" and not revoke_live:
                raise ApiError(
                    "event_claim_live",
                    "a live claim exists; resend with revoke_live_claim true",
                    details={"claimant": doc["claim"]["claimant"], "expires_at": doc["claim"]["expires_at"]},
                )
            ts = now_iso()
            doc["generation"] += 1
            doc["gen_started_at"] = ts
            doc["resend_mode"] = mode
            doc["claim_id"] = None
            doc["claim"] = None
            doc["handled_at"] = None
            doc["attempts"] = 0
            doc["last_attempt_at"] = None
            doc["last_status_code"] = None
            doc["last_error"] = None
            doc["delivered_at"] = None
            if mode == "from_start":
                doc["stages"] = []
            doc["body"] = body_bytes(self._body(doc)).decode("utf-8")
            self.db.execute(
                "UPDATE events SET state='pending', next_attempt_at=?, doc=? WHERE event_id=?", (ts, dumps(doc), event_id)
            )
        self.on_new()
        return self.record(self._row(event_id))

    def run_of(self, event_id: str) -> str:
        return self._row(event_id)["run_id"]

    def wake_pending_for_run(self, run_id: str) -> None:
        self.db.execute(
            "UPDATE events SET next_attempt_at=? WHERE run_id=? AND state='pending'", (now_iso(), run_id)
        )
        self.on_new()
