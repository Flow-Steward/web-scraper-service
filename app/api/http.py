"""HTTP conventions (README 1): request ids, bearer token, JSON bodies, errors, cursors."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.errors import ApiError, validation_failed
from app.core.ids import new_id
from app.core.schemas import schema_errors
from app.settings import get_settings

log = logging.getLogger("wss.http")


def request_id(request: Request) -> str:
    rid = getattr(request.state, "request_id", None)
    if rid is None:
        rid = new_id("req").replace("_", "_", 1)
        request.state.request_id = rid
    return rid


def error_response(request: Request, err: ApiError) -> JSONResponse:
    rid = request_id(request)
    headers = {"X-Request-Id": rid, **err.headers}
    return JSONResponse(err.body(rid), status_code=err.status, headers=headers)


class ApiMiddleware(BaseHTTPMiddleware):
    """Request id on every response; bearer token on every route (``/health`` included)."""

    async def dispatch(self, request: Request, call_next) -> Response:
        rid = request_id(request)
        settings = get_settings()
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth[:7].lower() == "bearer " else ""
        if not token or not hmac.compare_digest(token.encode(), settings.api_token.encode()):
            return error_response(request, ApiError("unauthorized", "missing or wrong bearer token"))
        started = time.monotonic()
        try:
            response = await call_next(request)
        except ApiError as err:  # raised outside the route handlers
            response = error_response(request, err)
        response.headers["X-Request-Id"] = rid
        log.info(
            "%s %s -> %s (%d ms) %s",
            request.method,
            request.url.path,
            response.status_code,
            int((time.monotonic() - started) * 1000),
            rid,
        )
        return response


def json_response(body: Any, status: int = 200, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers=headers)


async def read_json(request: Request, ref: str, *, allow_empty: bool = False) -> Any:
    settings = get_settings()
    ctype = request.headers.get("content-type", "")
    max_bytes = settings.limits["max_request_body_bytes"]
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise ApiError("request_too_large", f"the body is larger than {max_bytes} bytes")
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > max_bytes:
            raise ApiError("request_too_large", f"the body is larger than {max_bytes} bytes")
    if not body and allow_empty:
        data: Any = {}
    else:
        if ctype.split(";")[0].strip().lower() != "application/json":
            raise ApiError("unsupported_media_type", "the request body must be application/json")
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ApiError("invalid_json", "the body is not valid UTF-8 JSON") from exc
    errors = schema_errors(ref, data)
    if errors:
        raise validation_failed(errors)
    return data


def _cursor_key() -> bytes:
    return hashlib.sha256(("cursor:" + get_settings().api_token).encode()).digest()


def make_cursor(position: Any) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"p": position, "t": int(time.time())}, separators=(",", ":")).encode()).decode().rstrip("=")
    sig = hmac.new(_cursor_key(), payload.encode(), hashlib.sha256).hexdigest()[:16]
    return f"{payload}.{sig}"


def read_cursor(cursor: str | None) -> Any:
    if not cursor:
        return None
    try:
        payload, sig = cursor.rsplit(".", 1)
        expected = hmac.new(_cursor_key(), payload.encode(), hashlib.sha256).hexdigest()[:16]
        if not hmac.compare_digest(sig, expected):
            raise ValueError("signature")
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if time.time() - int(data["t"]) > 24 * 3600:
            raise ValueError("expired")
        return data["p"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ApiError("invalid_cursor", "the cursor is malformed or older than 24 hours") from exc


def read_limit(request: Request) -> int:
    raw = request.query_params.get("limit")
    if raw is None:
        return 50
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if not 1 <= value <= 200:
        from app.core.errors import field_error

        raise validation_failed([field_error("limit", "invalid_value", "limit must be 1..200")])
    return value
