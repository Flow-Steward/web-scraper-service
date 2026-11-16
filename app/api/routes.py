"""Every endpoint of the contract (``contract/openapi.yaml``)."""

from __future__ import annotations

import base64
import hashlib
import os
import platform as py_platform
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from app.api.http import json_response, make_cursor, read_cursor, read_json, read_limit
from app.core.config import apply_rules, dedupe_url_list, merge_layers, split_secret_requirements
from app.core.db import dumps, loads
from app.core.errors import ApiError, field_error, validation_failed
from app.core.schemas import schema_errors
from app.core.timeutil import now, now_iso, parse
from app.extraction.store import version_doc
from app.service import get_service
from app.settings import SERVICE_NAME, SERVICE_VERSION

router = APIRouter()

SITE_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*(:[0-9]{1,5})?$")
TEMPLATE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9:._-]{1,255}$")
RUN_ID_RE = re.compile(r"^run_[A-Za-z0-9]{1,64}$")
EVENT_ID_RE = re.compile(r"^evt_[A-Za-z0-9]{1,64}$")
MRQ_ID_RE = re.compile(r"^mrq_[A-Za-z0-9]{1,64}$")


def _check_id(value: str, pattern: re.Pattern, code: str, what: str) -> None:
    if not pattern.match(value):
        raise ApiError(code, f"unknown {what}")


def _versions() -> dict:
    import importlib.metadata as md

    def v(name: str) -> str | None:
        try:
            return md.version(name)
        except md.PackageNotFoundError:
            return None

    return {"python": py_platform.python_version(), "crawlee": v("crawlee") or "", "playwright": v("playwright") or "", "camoufox": v("camoufox")}


# ====================================================================== health
@router.get("/health")
async def health(request: Request):
    svc = get_service()
    s = svc.settings
    from app.fetching.browsers import browser_presence
    from app.fetching.solvers import PROVIDERS

    presence = browser_presence()
    problems = []
    if not svc.secrets.configured:
        problems.append("no job-secret key configured (WSS_SECRETS_KEY)")
    usage = shutil.disk_usage(s.data_dir)
    if usage.free < 2 * 1024**3:
        problems.append("free disk below 2 GiB")
    if not any(p["present"] for p in presence.values()):
        problems.append("no browser in the image")
    elif not presence["camoufox"]["present"]:
        problems.append("camoufox is not in this image")
    counts = {r["state"]: r["n"] for r in svc.db.all("SELECT state, COUNT(*) AS n FROM runs WHERE state IN ('running','stopping','queued','waiting_for_credentials') GROUP BY state")}
    body = {
        "status": "degraded" if problems else "ok",
        "time": now_iso(),
        "service": {"name": SERVICE_NAME, "version": SERVICE_VERSION, "api_versions": ["v1"], **({"build": s.build} if s.build else {})},
        "engine": _versions(),
        "browsers": [
            {"name": "camoufox", "present": presence["camoufox"]["present"], "version": presence["camoufox"]["version"]},
            {"name": "chromium", "present": presence["chromium"]["present"], "version": presence["chromium"]["version"]},
        ],
        "limits": dict(s.limits),
        "protection": {
            "challenge_click_allowed": s.challenge_click_allowed,
            "solvers_allowed": s.solvers_allowed,
            "robots_override_allowed": s.robots_override_allowed,
            "steps_allowed": s.protection_steps,
            "solver_providers": list(PROVIDERS),
            "solver_challenge_types": ["turnstile"],
        },
        "retention": {"result_days": s.result_days, "job_record_days": s.job_record_days, "stored_pages_days": s.stored_pages_days, "samples_hours": s.samples_hours},
        "timeouts": {"claim_timeout_seconds": s.claim_timeout_seconds, "mapping_request_timeout_seconds": s.mapping_request_timeout_seconds,
                     "stall_seconds": s.stall_seconds},
        "repair": {"window_pages": s.window_pages, "min_pages": s.min_pages, "no_template_pages": s.no_template_pages, "block_window_pages": s.block_window_pages, "block_share": s.block_share},
        "webhook_delivery": {
            "timeout_seconds": s.webhook_timeout_seconds,
            "schedule_seconds": list(s.webhook_schedule_seconds),
            "jitter": s.webhook_jitter,
            "signature_header": "X-Scraper-Signature",
            "timestamp_header": "X-Scraper-Timestamp",
        },
        "storage": {"free_bytes": usage.free, "total_bytes": usage.total},
        "secrets_key": {"configured": svc.secrets.configured, "undecryptable_jobs": svc.secrets.undecryptable_runs()},
        "jobs": {
            "running": counts.get("running", 0) + counts.get("stopping", 0),
            "queued": counts.get("queued", 0),
            "waiting_for_credentials": counts.get("waiting_for_credentials", 0),
        },
        "problems": problems,
    }
    return json_response(body)


# ====================================================================== config
@router.post("/v1/config/validate")
async def validate_config(request: Request):
    svc = get_service()
    body, secret_errors = await _read_split(request, "job_config.schema.json#/$defs/validate_request")
    profile = body["profile"]
    merged, sources = merge_layers(body.get("run_inputs"), body.get("site_settings"), (profile.get("defaults") or {}))
    if merged.get("urls"):
        merged["urls"] = dedupe_url_list(merged["urls"])
    from app.fetching.browsers import browser_presence

    fixed, errors, warnings = await apply_rules(merged, profile, svc.settings, browser_presence(), sources)
    errors = errors + secret_errors
    if not errors:
        root = schema_errors("job_config.schema.json", fixed)
        errors += root
    valid = not errors
    out = {"valid": valid, "config": fixed if valid else None, "errors": errors, "warnings": warnings, "sources": sources}
    return json_response(out)


async def _read_split(request: Request, ref: str) -> tuple[dict, list[dict]]:
    """Read and validate; missing secrets become ``secret_missing`` rule errors."""
    import json

    from app.settings import get_settings

    max_bytes = get_settings().limits["max_request_body_bytes"]
    ctype = request.headers.get("content-type", "")
    raw = b""
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > max_bytes:
            raise ApiError("request_too_large", f"the body is larger than {max_bytes} bytes")
    if ctype.split(";")[0].strip().lower() != "application/json":
        raise ApiError("unsupported_media_type", "the request body must be application/json")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ApiError("invalid_json", "the body is not valid UTF-8 JSON") from exc
    errors = schema_errors(ref, data)
    secret, other = split_secret_requirements(errors)
    if other:
        raise validation_failed(other + secret)
    return data, secret


# ====================================================================== setup
@router.post("/v1/analyze")
async def analyze(request: Request):
    from app.setup_ops import analyze as run

    body = await read_json(request, "analysis.schema.json#/$defs/analyze_request")
    return json_response(await run(get_service(), body))


@router.post("/v1/proxies/test")
async def proxies_test(request: Request):
    from app.setup_ops import proxy_test

    body, secret = await _read_split(request, "connection_profile.schema.json#/$defs/proxy_test_request")
    if secret:
        raise validation_failed(secret)
    return json_response(await proxy_test(get_service(), body))


@router.post("/v1/solvers/test")
async def solvers_test(request: Request):
    from app.setup_ops import solver_test

    body, secret = await _read_split(request, "connection_profile.schema.json#/$defs/solver_test_request")
    if secret:
        raise validation_failed(secret)
    return json_response(await solver_test(get_service(), body))


@router.post("/v1/samples")
async def samples(request: Request):
    from app.setup_ops import samples as run

    body = await read_json(request, "samples.schema.json#/$defs/samples_request")
    return json_response(await run(get_service(), body))


@router.post("/v1/mappings/test")
async def mappings_test(request: Request):
    from app.setup_ops import mapping_test

    body = await read_json(request, "mapping_test.schema.json#/$defs/request")
    return json_response(await mapping_test(get_service(), body))


# ====================================================================== mappings
@router.get("/v1/sites")
async def list_sites(request: Request):
    svc = get_service()
    limit = read_limit(request)
    after = read_cursor(request.query_params.get("cursor"))
    sites = svc.mappings.sites()
    if after:
        sites = [s for s in sites if s["site"] > after]
    page = sites[:limit]
    nxt = make_cursor(page[-1]["site"]) if len(sites) > limit else None
    return json_response({"items": page, "next_cursor": nxt})


@router.get("/v1/sites/{site}/mappings")
async def get_site_mappings(site: str, request: Request):
    svc = get_service()
    errors = []
    if not SITE_RE.match(site):
        errors.append(field_error("site", "invalid_value", "not a site key"))
    tid = request.query_params.get("template_id")
    if tid is not None and not TEMPLATE_RE.match(tid):
        errors.append(field_error("template_id", "invalid_value", "not a template id"))
    versions = request.query_params.get("versions", "active")
    if versions not in ("active", "all"):
        errors.append(field_error("versions", "invalid_value", "versions must be active or all"))
    include = request.query_params.get("include_content", "true").lower()
    if include not in ("true", "false", "1", "0"):
        errors.append(field_error("include_content", "invalid_value", "include_content must be a boolean"))
    if errors:
        raise validation_failed(errors)
    templates = svc.mappings.site_templates(site, tid, versions, include in ("true", "1"))
    return json_response({"site": site, "templates": templates})


@router.put("/v1/sites/{site}/mappings")
async def activate_mapping(site: str, request: Request):
    from app.setup_ops import validate_mapping_rules

    svc = get_service()
    body = await read_json(request, "mapping.schema.json#/$defs/activate_request")
    errors = []
    if not SITE_RE.match(site):
        errors.append(field_error("site", "invalid_value", "not a site key"))
    tid = body["template_id"]
    mapping = body["mapping"]
    if mapping["template_id"] != tid:
        errors.append(field_error("template_id", "template_id_mismatch", "template_id differs from mapping.template_id"))
    errors += validate_mapping_rules(mapping, svc, "mapping")
    if errors:
        raise validation_failed(errors)
    resolved: list[str] = []
    with svc.db.tx():
        previous = svc.mappings.active(site, tid)
        if previous is None and svc.mappings.active_template_count(site) >= svc.settings.limits["max_templates_per_site"]:
            raise validation_failed([field_error("template_id", "template_limit_reached", "the site has the maximum number of templates", svc.settings.limits["max_templates_per_site"])])
        outcome, value = svc.mappings.activate(site, tid, body["expected_version"], mapping, body.get("origin"))
        if outcome == "conflict":
            raise ApiError("mapping_version_conflict", f"expected_version {body['expected_version']} is not the active version {value}", details={"active_version": value})
        row = value
        if outcome == "changed":
            new_template = previous is None
            for req in svc.db.all("SELECT mapping_request_id, doc, template_id FROM mapping_requests WHERE status='open'"):
                d = loads(req["doc"])
                if d["site"] != site:
                    continue
                if req["template_id"] == tid or (req["template_id"] is None and new_template):
                    d["status"] = "resolved"
                    d["closed_at"] = now_iso()
                    d["resolution"] = {"status": "resolved", "version": int(row["version"]), "failure_reason": None}
                    svc.db.execute("UPDATE mapping_requests SET status='resolved', doc=? WHERE mapping_request_id=?", (dumps(d), req["mapping_request_id"]))
                    resolved.append(req["mapping_request_id"])
    if outcome == "changed":
        svc.jobs.notify_activation(site, tid, int(row["version"]), mapping, resolved, previous is None)
    out = {"site": site, "template_id": tid, "changed": outcome == "changed", "active": version_doc(row)}
    if outcome == "changed":
        out["resolved_mapping_request_ids"] = resolved
    return json_response(out)


# ====================================================================== runs
@router.post("/v1/runs")
async def start_run(request: Request):
    svc = get_service()
    key = request.headers.get("idempotency-key")
    if key is None:
        raise ApiError("idempotency_key_required", "POST /v1/runs needs an Idempotency-Key header")
    if not IDEMPOTENCY_RE.match(key):
        raise ApiError("idempotency_key_invalid", "the Idempotency-Key does not match ^[A-Za-z0-9:._-]{1,255}$")
    if svc.shutting_down:
        raise ApiError("service_unavailable", "the service is shutting down", headers={"Retry-After": "30"})
    body, secret = await _read_split(request, "run_summary.schema.json#/$defs/run_create_request")
    if secret:
        raise validation_failed(secret)
    if body["config"].get("urls"):
        body["config"]["urls"] = dedupe_url_list(body["config"]["urls"])
    status, summary = await svc.jobs.create(body, key)
    return json_response(summary, status=status, headers={"Location": f"/v1/runs/{summary['run_id']}"})


@router.get("/v1/runs")
async def list_runs(request: Request):
    svc = get_service()
    limit = read_limit(request)
    after = read_cursor(request.query_params.get("cursor"))
    q = request.query_params
    sql = "SELECT * FROM runs WHERE 1=1"
    params: list = []
    errors = []
    if q.get("state"):
        if q["state"] not in ("queued", "running", "waiting_for_credentials", "stopping", "completed", "failed"):
            errors.append(field_error("state", "invalid_value", "unknown state"))
        sql += " AND state=?"
        params.append(q["state"])
    if q.get("site"):
        if not SITE_RE.match(q["site"]):
            errors.append(field_error("site", "invalid_value", "not a site key"))
        sql += " AND site=?"
        params.append(q["site"])
    for name, col in (("project_id", "project_id"), ("connection_id", "connection_id"), ("platform_job_id", "platform_job_id")):
        if q.get(name):
            sql += f" AND {col}=?"
            params.append(q[name])
    if errors:
        raise validation_failed(errors)
    if after:
        sql += " AND (created_at < ? OR (created_at = ? AND run_id < ?))"
        params += [after[0], after[0], after[1]]
    sql += " ORDER BY created_at DESC, run_id DESC LIMIT ?"
    params.append(limit + 1)
    rows = svc.db.all(sql, tuple(params))
    items = [svc.jobs.list_item(r) for r in rows[:limit]]
    nxt = make_cursor([rows[limit - 1]["created_at"], rows[limit - 1]["run_id"]]) if len(rows) > limit else None
    return json_response({"items": items, "next_cursor": nxt})


@router.get("/v1/runs/{run_id}")
async def get_run(run_id: str):
    _check_id(run_id, RUN_ID_RE, "run_not_found", "run_id")
    svc = get_service()
    summary = svc.jobs.summary(run_id)
    runner = svc.jobs.runners.get(run_id)
    if runner is not None and summary["state"] in ("running", "stopping"):
        pending, in_progress = await runner.queue_counts()
        summary["progress"]["queue_pending"] = pending
        summary["progress"]["in_progress"] = in_progress
    return json_response(summary)


@router.get("/v1/runs/{run_id}/unextracted")
async def list_unextracted(run_id: str, request: Request):
    _check_id(run_id, RUN_ID_RE, "run_not_found", "run_id")
    svc = get_service()
    svc.jobs._row(run_id)
    accept = request.headers.get("accept", "application/json")
    ndjson = "application/x-ndjson" in accept
    if not ndjson and not any(t in accept for t in ("application/json", "*/*", "application/*")):
        raise ApiError("not_acceptable", "the route produces application/json or application/x-ndjson")
    q = request.query_params
    where = "run_id=?"
    params: list = [run_id]
    errors = []
    if q.get("reason"):
        if q["reason"] not in ("fetch_failed", "not_found", "blocked", "incomplete", "no_template", "template_failed", "stopped", "robots_disallowed", "private_address_refused"):
            errors.append(field_error("reason", "invalid_value", "unknown reason"))
        where += " AND reason=?"
        params.append(q["reason"])
    if q.get("template_id"):
        where += " AND template_id=?"
        params.append(q["template_id"])
    if errors:
        raise validation_failed(errors)
    if ndjson:
        rows = svc.db.all(f"SELECT doc FROM unextracted WHERE {where} ORDER BY url, url_key", tuple(params))

        def gen():
            for r in rows:
                yield (r["doc"] + "\n").encode("utf-8")

        return StreamingResponse(gen(), media_type="application/x-ndjson")
    limit = read_limit(request)
    after = read_cursor(q.get("cursor"))
    total = int(svc.db.scalar(f"SELECT COUNT(*) FROM unextracted WHERE {where}", tuple(params)) or 0)
    page_where = where
    page_params = list(params)
    if after:
        page_where += " AND (url > ? OR (url = ? AND url_key > ?))"
        page_params += [after[0], after[0], after[1]]
    rows = svc.db.all(f"SELECT url, url_key, doc FROM unextracted WHERE {page_where} ORDER BY url, url_key LIMIT ?", tuple(page_params + [limit + 1]))
    items = [loads(r["doc"]) for r in rows[:limit]]
    nxt = make_cursor([rows[limit - 1]["url"], rows[limit - 1]["url_key"]]) if len(rows) > limit else None
    return json_response({"items": items, "next_cursor": nxt, "total": total})


@router.post("/v1/runs/{run_id}/stop")
async def stop_run(run_id: str, request: Request):
    _check_id(run_id, RUN_ID_RE, "run_not_found", "run_id")
    svc = get_service()
    svc.jobs._row(run_id)
    await read_json(request, "run_summary.schema.json#/$defs/run_stop_request", allow_empty=True)
    return json_response(await svc.jobs.stop(run_id))


@router.post("/v1/runs/{run_id}/resume")
async def resume_run(run_id: str, request: Request):
    _check_id(run_id, RUN_ID_RE, "run_not_found", "run_id")
    svc = get_service()
    svc.jobs._row(run_id)
    body, secret = await _read_split(request, "run_summary.schema.json#/$defs/run_resume_request")
    if secret:
        raise validation_failed(secret)
    return json_response(await svc.jobs.resume(run_id, body["profile"]))


@router.post("/v1/runs/{run_id}/recollect")
async def recollect_run(run_id: str, request: Request):
    _check_id(run_id, RUN_ID_RE, "run_not_found", "run_id")
    svc = get_service()
    key = request.headers.get("idempotency-key")
    if key is None:
        raise ApiError("idempotency_key_required", "POST /v1/runs/{id}/recollect needs an Idempotency-Key header")
    if not IDEMPOTENCY_RE.match(key):
        raise ApiError("idempotency_key_invalid", "the Idempotency-Key does not match ^[A-Za-z0-9:._-]{1,255}$")
    if svc.shutting_down:
        raise ApiError("service_unavailable", "the service is shutting down", headers={"Retry-After": "30"})
    svc.jobs._row(run_id)
    body, secret = await _read_split(request, "run_summary.schema.json#/$defs/run_recollect_request")
    if secret:
        raise validation_failed(secret)
    status, summary = await svc.jobs.recollect(run_id, body, key)
    return json_response(summary, status=status, headers={"Location": f"/v1/runs/{summary['run_id']}"})


def _result_doc(run_id: str) -> dict:
    svc = get_service()
    row = svc.jobs._row(run_id)
    doc = loads(row["doc"])
    if row["state"] == "failed":
        raise ApiError("result_not_available", "the run failed; it has no result")
    if row["state"] != "completed" or not doc.get("result"):
        raise ApiError("result_not_ready", "the run is not completed yet", details={"state": row["state"]})
    result = doc["result"]
    if parse(result["expires_at"]) <= now():
        raise ApiError("result_expired", "the result file's retention has ended")
    return result


@router.get("/v1/runs/{run_id}/result")
async def get_result(run_id: str):
    _check_id(run_id, RUN_ID_RE, "run_not_found", "run_id")
    return json_response(dict(_result_doc(run_id), fetched_at=_fetched_at(run_id, "result")))


def _format(request: Request) -> str:
    fmt = request.query_params.get("format", "jsonl")
    if fmt not in ("jsonl", "csv"):
        raise validation_failed([field_error("format", "invalid_value", "format must be jsonl or csv")])
    return fmt


def _mark_fetched(run_id: str, kind: str) -> None:
    get_service().db.execute("INSERT OR IGNORE INTO file_fetches(run_id, kind, fetched_at) VALUES (?, ?, ?)", (run_id, kind, now_iso()))


def _fetched_at(run_id: str, kind: str) -> str | None:
    """When the file's last byte first went out in a download answer (either format); null before."""
    return get_service().db.scalar("SELECT fetched_at FROM file_fetches WHERE run_id=? AND kind=?", (run_id, kind))


def _serve_file(request: Request, path: Path, file_name: str, size: int, sha256: str, media_type: str, on_fetched) -> Response:
    """One immutable file with ETag, Repr-Digest and single byte ranges (README 7.2). ``on_fetched``
    runs once the file's last byte has gone out (or on a 304 for its ETag): README 16.3."""
    if not path.exists():
        raise ApiError("result_expired", "the file is gone")
    etag = f'"{sha256}"'
    digest = base64.b64encode(bytes.fromhex(sha256)).decode()
    headers = {
        "Accept-Ranges": "bytes",
        "ETag": etag,
        "Content-Disposition": f'attachment; filename="{file_name}"',
        "Repr-Digest": f"sha-256=:{digest}:",
    }
    inm = request.headers.get("if-none-match")
    if inm and (inm.strip() == etag or inm.strip() == "*" or etag in [x.strip() for x in inm.split(",")]):
        on_fetched()  # the client holds the whole file already
        return Response(status_code=304, headers=headers)
    rng = request.headers.get("range")
    if_range = request.headers.get("if-range")
    if rng and if_range and if_range.strip() != etag:
        rng = None
    start, end = 0, size - 1
    partial = False
    if rng:
        m = re.fullmatch(r"\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*", rng)
        if "," in rng:
            m = None  # several ranges: whole file
        if m and (m.group(1) or m.group(2)):
            a, b = m.group(1), m.group(2)
            if a:
                start = int(a)
                end = min(int(b), size - 1) if b else size - 1
                if b and int(b) < start:
                    raise ApiError("range_not_satisfiable", "invalid range", headers={"Content-Range": f"bytes */{size}"})
            else:
                n = int(b)
                if n == 0:
                    raise ApiError("range_not_satisfiable", "empty suffix range", headers={"Content-Range": f"bytes */{size}"})
                start = max(0, size - n)
                end = size - 1
            if start >= size:
                raise ApiError("range_not_satisfiable", "the range is outside the file", headers={"Content-Range": f"bytes */{size}"})
            partial = True
    length = end - start + 1 if size else 0

    async def stream():
        with open(path, "rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
        if (remaining == 0 and end == size - 1) or size == 0:
            on_fetched()

    headers["Content-Length"] = str(length)
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        return StreamingResponse(stream(), status_code=206, media_type=media_type, headers=headers)
    return StreamingResponse(stream(), status_code=200, media_type=media_type, headers=headers)


CSV_MEDIA = "text/csv; charset=utf-8"


@router.get("/v1/runs/{run_id}/result/download")
async def download_result(run_id: str, request: Request):
    _check_id(run_id, RUN_ID_RE, "run_not_found", "run_id")
    fmt = _format(request)
    svc = get_service()
    result = _result_doc(run_id)
    if fmt == "csv":
        if not result.get("csv"):
            raise ApiError("result_expired", "this run has no CSV file")
        f = result["csv"]
        return _serve_file(request, Path(svc.settings.data_dir) / "results" / f["file_name"], f["file_name"], f["size_bytes"], f["sha256"], CSV_MEDIA,
                           lambda: _mark_fetched(run_id, "result"))
    return _serve_file(request, Path(svc.settings.data_dir) / "results" / result["file_name"], result["file_name"], result["size_bytes"], result["sha256"],
                       "application/x-ndjson", lambda: _mark_fetched(run_id, "result"))


# ====================================================================== admin (README 16)
@router.get("/v1/admin/storage")
async def admin_storage():
    from app.jobs.housekeeping import storage

    return json_response(await storage(get_service()))


@router.post("/v1/admin/cleanup")
async def admin_cleanup(request: Request):
    from app.jobs.housekeeping import cleanup

    body = await read_json(request, "cleanup.schema.json#/$defs/cleanup_request", allow_empty=True)
    return json_response(await cleanup(get_service(), body or {}))


# ====================================================================== mapping requests
def _mrq(mrq_id: str):
    _check_id(mrq_id, MRQ_ID_RE, "mapping_request_not_found", "mapping_request_id")
    svc = get_service()
    row = svc.db.one("SELECT * FROM mapping_requests WHERE mapping_request_id=?", (mrq_id,))
    if row is None:
        raise ApiError("mapping_request_not_found", "unknown mapping_request_id")
    d = loads(row["doc"])
    if d["status"] == "open" and parse(d["expires_at"]) <= now():
        d["status"] = "expired"
        d["closed_at"] = now_iso()
        d["resolution"] = {"status": "expired", "version": None, "failure_reason": "timeout"}
        svc.db.execute("UPDATE mapping_requests SET status='expired', doc=? WHERE mapping_request_id=?", (dumps(d), mrq_id))
        svc.jobs.notify_request_closed(row["run_id"], mrq_id, "expired")
    return row, d


@router.get("/v1/screenshots/{screenshot_id}")
async def get_screenshot(screenshot_id: str):
    """A screenshot kept by an analysis or samples request with ``debug: true`` (PNG, at most
    4 MiB, same retention as samples)."""
    png = get_service().samples.load_shot(screenshot_id)
    if png is None:
        raise ApiError("screenshot_not_found", "unknown or expired screenshot id")
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.get("/v1/mapping-requests/{mapping_request_id}")
async def get_mapping_request(mapping_request_id: str):
    _row, d = _mrq(mapping_request_id)
    return json_response(d)


@router.post("/v1/mapping-requests/{mapping_request_id}/fail")
async def fail_mapping_request(mapping_request_id: str, request: Request):
    svc = get_service()
    row, d = _mrq(mapping_request_id)
    body = await read_json(request, "mapping_request.schema.json#/$defs/fail_request")
    if d["status"] == "failed":
        return json_response(d)
    if d["status"] != "open":
        raise ApiError("mapping_request_closed", f"the mapping request is {d['status']}", details={"status": d["status"]})
    d["status"] = "failed"
    d["closed_at"] = now_iso()
    d["resolution"] = {"status": "failed", "version": None, "failure_reason": body["failure_reason"]}
    svc.db.execute("UPDATE mapping_requests SET status='failed', doc=? WHERE mapping_request_id=?", (dumps(d), mapping_request_id))
    svc.jobs.notify_request_closed(row["run_id"], mapping_request_id, "failed")
    return json_response(d)


# ====================================================================== events
@router.post("/v1/events/{event_id}/claim")
async def claim_event(event_id: str, request: Request):
    _check_id(event_id, EVENT_ID_RE, "event_not_found", "event_id")
    body = await read_json(request, "event.schema.json#/$defs/claim_request")
    return json_response(get_service().events.claim(event_id, body["claimant"]))


@router.post("/v1/events/{event_id}/stage")
async def stage_event(event_id: str, request: Request):
    _check_id(event_id, EVENT_ID_RE, "event_not_found", "event_id")
    body = await read_json(request, "event.schema.json#/$defs/stage_request")
    return json_response(get_service().events.stage(event_id, body["claim_id"], body["stage"]))


@router.post("/v1/events/{event_id}/complete")
async def complete_event(event_id: str, request: Request):
    _check_id(event_id, EVENT_ID_RE, "event_not_found", "event_id")
    body = await read_json(request, "event.schema.json#/$defs/complete_request")
    return json_response(get_service().events.complete(event_id, body["claim_id"]))


@router.post("/v1/events/{event_id}/resend")
async def resend_event(event_id: str, request: Request):
    _check_id(event_id, EVENT_ID_RE, "event_not_found", "event_id")
    svc = get_service()
    body, secret = await _read_split(request, "event.schema.json#/$defs/resend_request")
    if secret:
        raise validation_failed(secret)
    run_id = svc.events.run_of(event_id)
    if not svc.secrets.configured:
        raise ApiError("secrets_key_unavailable", "the service has no job-secret encryption key configured", headers={"Retry-After": "300"})
    record = svc.events.resend(event_id, body["mode"], bool(body.get("revoke_live_claim")), body.get("expected_generation"))
    svc.secrets.put(run_id, "webhook", body["webhook"])
    svc.events.wake_pending_for_run(run_id)
    return json_response(svc.events.record(svc.db.one("SELECT * FROM events WHERE event_id=?", (event_id,))))
