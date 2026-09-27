"""FastAPI application factory with middleware and error handlers (T014)."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from audr.api.auth import router as auth_router
from audr.api.errors import unhandled_exception_handler
from audr.api.wallets import router as wallets_router

logger = logging.getLogger(__name__)

_PRIVATE_PREFIXES = ("/api/",)


def create_app() -> FastAPI:
    app = FastAPI(
        title="audr",
        description="Ethereum portfolio tracker API",
        docs_url=None,
        redoc_url=None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def attach_request_id(request: Request, call_next: Callable) -> Response:
        rid = uuid.uuid4()
        request.state.request_id = rid
        logger.info(
            "request path=%s method=%s request_id=%s",
            request.url.path,
            request.method,
            rid,
        )
        response = await call_next(request)
        response.headers["X-Request-Id"] = str(rid)
        return response

    @app.middleware("http")
    async def no_store_private_routes(request: Request, call_next: Callable) -> Response:
        response = await call_next(request)
        if any(request.url.path.startswith(p) for p in _PRIVATE_PREFIXES):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.add_exception_handler(Exception, unhandled_exception_handler)

    app.include_router(auth_router)
    app.include_router(wallets_router)

    return app


app = create_app()
