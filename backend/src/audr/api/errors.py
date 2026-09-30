"""Sanitized API error handlers and the contract error envelope (T014, AUD-320).

Every error response — 4xx raised by route code, 422 from request validation,
and unhandled 5xx — is rendered as the envelope mandated by
`specs/001-ethereum-portfolio/contracts/http-api.md`:

    {"error": {"code", "message", "field_errors", "retryable"}, "request_id"}

The frontend API client (`frontend/src/api/client.ts`) reads
`body.error.message`; before this was standardised every failure rendered in the
UI as a bare "HTTP nnn".
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

# Status code → contract error code. Anything unmapped becomes `http_<status>`
# rather than a misleading generic code.
_CODE_BY_STATUS: dict[int, str] = {
    400: "bad_request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "invalid_value",
    429: "throttled",
    500: "internal_server_error",
    503: "unavailable",
}

_MESSAGE_BY_STATUS: dict[int, str] = {
    400: "Malformed request.",
    401: "Authentication required.",
    403: "Request denied.",
    404: "Resource not found.",
    405: "Method not allowed.",
    409: "Conflicting state.",
    422: "Invalid value.",
    429: "Too many requests.",
    500: "An unexpected error occurred.",
    503: "Service unavailable.",
}


class ErrorBody(BaseModel):
    code: str
    message: str
    field_errors: dict[str, str] = Field(default_factory=dict)
    retryable: bool = False


class ErrorEnvelope(BaseModel):
    error: ErrorBody
    request_id: str


def _request_id(request: Request) -> str:
    return (
        str(request.state.request_id)
        if hasattr(request.state, "request_id")
        else "unknown"
    )


def _retryable(status_code: int) -> bool:
    return status_code in (429, 503) or status_code >= 500


def error_envelope(
    request: Request,
    status_code: int,
    *,
    code: str | None = None,
    message: str | None = None,
    field_errors: dict[str, str] | None = None,
    retryable: bool | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the contract error envelope for a response body."""
    body = ErrorBody(
        code=code or _CODE_BY_STATUS.get(status_code, f"http_{status_code}"),
        message=message or _MESSAGE_BY_STATUS.get(status_code, "Request failed."),
        field_errors=field_errors or {},
        retryable=_retryable(status_code) if retryable is None else retryable,
    )
    payload = ErrorEnvelope(error=body, request_id=_request_id(request)).model_dump()
    if extra:
        # Route-supplied diagnostics (e.g. the conflicting asset's existing_id,
        # or the readiness subsystem statuses) ride alongside code/message.
        payload["error"].update(extra)
    return payload


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render FastAPI/Starlette HTTPExceptions as the contract envelope.

    `HTTPException(detail=...)` is accepted in three shapes:
      - a string → used as `message`
      - a dict carrying `code`/`message` → used directly, remaining keys as extras
      - any other dict → status-derived code/message, all keys as extras
    """
    assert isinstance(exc, StarletteHTTPException)  # registered for this type only
    code: str | None = None
    message: str | None = None
    field_errors: dict[str, str] | None = None
    extra: dict[str, Any] = {}

    detail = exc.detail
    if isinstance(detail, dict):
        remaining = dict(detail)
        raw_code = remaining.pop("code", None)
        raw_message = remaining.pop("message", None)
        raw_fields = remaining.pop("field_errors", None)
        code = str(raw_code) if raw_code is not None else None
        message = str(raw_message) if raw_message is not None else None
        if isinstance(raw_fields, dict):
            field_errors = {str(k): str(v) for k, v in raw_fields.items()}
        extra = remaining
    elif detail is not None:
        message = str(detail)

    return JSONResponse(
        status_code=exc.status_code,
        content=error_envelope(
            request,
            exc.status_code,
            code=code,
            message=message,
            field_errors=field_errors,
            extra=extra,
        ),
        # Preserve WWW-Authenticate, Retry-After and friends.
        headers=dict(exc.headers) if exc.headers else None,
    )


async def validation_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    """Render request-validation failures as 422 with per-field messages."""
    assert isinstance(exc, RequestValidationError)  # registered for this type only
    field_errors: dict[str, str] = {}
    for err in exc.errors():
        loc = [str(part) for part in err.get("loc", ()) if part != "body"]
        field_errors[".".join(loc) or "body"] = str(err.get("msg", "invalid value"))

    return JSONResponse(
        status_code=422,
        content=error_envelope(
            request,
            422,
            message="Request validation failed.",
            field_errors=field_errors,
        ),
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error request_id=%s", _request_id(request))
    # Never echo the exception text — it may carry provider URLs or credentials.
    return JSONResponse(status_code=500, content=error_envelope(request, 500))
