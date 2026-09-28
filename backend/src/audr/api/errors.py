"""Sanitized API error handlers and response schema (T014)."""

from __future__ import annotations

import logging

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class ErrorDetail(BaseModel):
    request_id: str
    error: str
    detail: str | None = None


def _request_id(request: Request) -> str:
    return str(request.state.request_id) if hasattr(request.state, "request_id") else "unknown"


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    rid = _request_id(request)
    logger.exception("Unhandled error request_id=%s", rid)
    return JSONResponse(
        status_code=500,
        content=ErrorDetail(
            request_id=rid,
            error="internal_server_error",
            detail="An unexpected error occurred.",
        ).model_dump(),
    )


async def http_404_handler(request: Request, exc: Exception) -> JSONResponse:
    rid = _request_id(request)
    return JSONResponse(
        status_code=404,
        content=ErrorDetail(
            request_id=rid,
            error="not_found",
        ).model_dump(),
    )
