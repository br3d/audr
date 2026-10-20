"""FastAPI application factory with middleware and error handlers (T014)."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from audr.api.alerts import router as alerts_router
from audr.api.assets import router as assets_router
from audr.api.auth import router as auth_router
from audr.api.errors import (
    http_exception_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from audr.api.events import router as events_router
from audr.api.health import router as health_router
from audr.api.history import router as history_router
from audr.api.holdings import router as holdings_router
from audr.api.integrations import router as integrations_router
from audr.api.llm import router as llm_router
from audr.api.networks import router as networks_router
from audr.api.news import router as news_router
from audr.api.openapi_curated import build_curated_openapi
from audr.api.portfolio import router as portfolio_router
from audr.api.settings import router as settings_router
from audr.api.spa import mount_spa
from audr.api.tokens import router as tokens_router
from audr.api.version import router as version_router
from audr.api.wallets import router as wallets_router
from audr.api.wellknown import router as wellknown_router
from audr.config import get_spa_settings
from audr.version import __version__

logger = logging.getLogger(__name__)

_PRIVATE_PREFIXES = ("/api/",)


def create_app() -> FastAPI:
    app = FastAPI(
        title="audr",
        description="Ethereum portfolio tracker API",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        # FastAPI's default auto-generated schema describes every route,
        # including the session-only operator surfaces — publishing that at
        # the conventional /openapi.json would be a security regression, not
        # a docs feature. openapi_url=None kills the default route; the
        # curated replacement below takes the same path (AUD-633).
        openapi_url=None,
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
        # A route that already set its own Cache-Control (e.g. the cached
        # asset icon route, AUD-385) knows better than this blanket default —
        # don't overwrite it.
        if (
            any(request.url.path.startswith(p) for p in _PRIVATE_PREFIXES)
            and "Cache-Control" not in response.headers
        ):
            response.headers["Cache-Control"] = "no-store"
        return response

    # All three handlers emit the contract error envelope (AUD-320): without the
    # HTTPException/validation handlers every 4xx fell through to FastAPI's
    # {"detail": ...} default, which the SPA renders as a bare "HTTP nnn".
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)

    app.include_router(health_router)
    app.include_router(version_router)
    app.include_router(auth_router)
    app.include_router(wallets_router)
    app.include_router(holdings_router)
    app.include_router(portfolio_router)
    app.include_router(assets_router)
    app.include_router(history_router)
    app.include_router(integrations_router)
    app.include_router(networks_router)
    app.include_router(settings_router)
    app.include_router(events_router)
    app.include_router(news_router)
    app.include_router(alerts_router)
    app.include_router(tokens_router)
    app.include_router(llm_router)
    app.include_router(wellknown_router)

    # Unauthenticated on purpose (AUD-633): this is API *shape*, not
    # portfolio data, curated down to the token-readable routes above so it
    # can safely sit outside the session/token gate. See
    # audr.api.openapi_curated for what gets filtered out and why.
    @app.get("/openapi.json", include_in_schema=False)
    async def get_curated_openapi() -> dict[str, Any]:
        return build_curated_openapi(app)

    # Last, and only last: this mounts a catch-all at `/` for the built SPA
    # (AUD-388, replacing the nginx `web` container). Every router above must be
    # registered first so it keeps winning the route match.
    spa_settings = get_spa_settings()
    if spa_settings.serve_spa:
        mount_spa(app, spa_settings.spa_dir)

    return app


app = create_app()
