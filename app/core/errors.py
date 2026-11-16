"""Error envelope (README 11)."""

from __future__ import annotations

from typing import Any

HTTP_STATUS = {
    "invalid_json": 400,
    "idempotency_key_required": 400,
    "idempotency_key_invalid": 400,
    "invalid_cursor": 400,
    "unauthorized": 401,
    "not_found": 404,
    "run_not_found": 404,
    "event_not_found": 404,
    "mapping_request_not_found": 404,
    "sample_not_found": 404,
    "screenshot_not_found": 404,
    "method_not_allowed": 405,
    "not_acceptable": 406,
    "idempotency_key_reused": 409,
    "mapping_version_conflict": 409,
    "event_already_claimed": 409,
    "event_already_handled": 409,
    "event_stale": 409,
    "claim_superseded": 409,
    "event_claim_live": 409,
    "event_generation_conflict": 409,
    "stage_out_of_order": 409,
    "mapping_request_closed": 409,
    "result_not_ready": 409,
    "result_not_available": 409,
    "run_not_finished": 409,
    "nothing_to_recollect": 409,
    "site_busy": 409,
    "result_expired": 410,
    "request_too_large": 413,
    "unsupported_media_type": 415,
    "range_not_satisfiable": 416,
    "validation_failed": 422,
    "queue_full": 429,
    "internal_error": 500,
    "service_unavailable": 503,
    "secrets_key_unavailable": 503,
}


class ApiError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        fields: list[dict[str, Any]] | None = None,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fields = fields
        self.details = details
        self.headers = headers or {}

    @property
    def status(self) -> int:
        return HTTP_STATUS[self.code]

    def body(self, request_id: str) -> dict[str, Any]:
        err: dict[str, Any] = {"code": self.code, "message": self.message[:2000], "request_id": request_id}
        if self.fields is not None:
            err["fields"] = self.fields
        if self.details is not None:
            err["details"] = self.details
        return {"error": err}


def field_error(field: str, code: str, message: str, limit: Any = None) -> dict[str, Any]:
    item: dict[str, Any] = {"field": field, "code": code, "message": message[:2000]}
    if limit is not None:
        item["limit"] = limit
    return item


def validation_failed(fields: list[dict[str, Any]], message: str = "The request is not valid") -> ApiError:
    return ApiError("validation_failed", message, fields=fields)
