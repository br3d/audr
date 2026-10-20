"""FastAPI routes for setup, authentication, session, and password management."""

from __future__ import annotations

import dataclasses
import re
import uuid
from datetime import UTC
from typing import Annotated
from urllib.parse import urlparse
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, Response
from fastapi import status as http_status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from audr.auth.models import Session
from audr.auth.service import (
    AlreadySetupError,
    AuthenticationError,
    NotSetupError,
    ThrottledError,
    change_password,
    get_valid_session,
    is_setup,
    login,
    revoke_session,
    setup_owner,
    verify_bearer_token,
)
from audr.db import get_db

# ---------------------------------------------------------------------------
# Token auth context and allow-list
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class TokenContext:
    """Represents a successfully authenticated API token request."""

    token_id: uuid.UUID
    redact_addresses: bool


# Union type returned by _require_read_auth — either a live Session or a
# validated TokenContext.  Route functions that don't use the value can use
# `_auth` (underscore prefix).
ReadPrincipal = Session | TokenContext

# Paths (prefix-matched) that Bearer tokens may access.  Everything else
# returns 403 — route allow-list, not deny-list.
_TOKEN_ALLOWED_PREFIXES: frozenset[str] = frozenset(
    {
        "/api/v1/portfolio",
        "/api/v1/holdings",
        "/api/v1/assets",
        "/api/v1/history",
        "/api/v1/events",
        "/api/v1/allowances",
        "/api/v1/llm",
        "/api/v1/news",
        "/api/v1/alerts",
        "/api/v1/status",
        "/api/v1/version",
    }
)


# GET /api/v1/wallets/{wallet_id}/holdings lives under /wallets, not /holdings,
# but is a "holdings" read for allow-list purposes — matched explicitly so the
# rest of /api/v1/wallets (list, detail, label, stop/reactivate) stays out of
# token reach.
_WALLET_HOLDINGS_PATH = re.compile(r"^/api/v1/wallets/[^/]+/holdings$")


def _is_token_allowed_path(path: str) -> bool:
    if _WALLET_HOLDINGS_PATH.match(path):
        return True
    return any(path.startswith(prefix) for prefix in _TOKEN_ALLOWED_PREFIXES)


router = APIRouter(prefix="/api/v1")

_COOKIE_NAME = "sid"
_SESSION_MAX_AGE = 86400  # 24 hours in seconds


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class PasswordBody(BaseModel):
    password: str = Field(min_length=12, max_length=128)


class LoginBody(BaseModel):
    """Login accepts any candidate password; the length floor is a setup-time
    complexity rule, not a login-time one — a wrong password must fail with
    401 from the credential check, never 422 from schema validation."""

    password: str = Field(min_length=1, max_length=128)


class ChangePasswordBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=12, max_length=128)


class SetupStatusResponse(BaseModel):
    setup_required: bool


class TokenResponse(BaseModel):
    csrf_token: str


class SessionResponse(BaseModel):
    authenticated: bool = True
    expires_at: str
    csrf_token: str


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _set_session_cookie(response: Response, session_id: UUID) -> None:
    response.set_cookie(
        key=_COOKIE_NAME,
        value=str(session_id),
        httponly=True,
        samesite="lax",
        path="/",
        max_age=_SESSION_MAX_AGE,
        secure=False,
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=_COOKIE_NAME, path="/")


# ---------------------------------------------------------------------------
# CSRF + origin guard: used as a dependency for state-mutating routes
# ---------------------------------------------------------------------------


async def _require_csrf(
    request: Request,
    x_csrf_token: Annotated[str | None, Header(alias="x-csrf-token")] = None,
    sid: Annotated[str | None, Cookie(alias="sid")] = None,
    db: AsyncSession = Depends(get_db),
) -> Session:
    """Validate session cookie + CSRF token; reject cross-origin mutations."""
    # Origin check: if Origin header is present it must match the Host.
    origin = request.headers.get("origin")
    if origin is not None:
        host = request.headers.get("host", "")
        origin_host = urlparse(origin).netloc
        if origin_host != host:
            raise HTTPException(
                status_code=http_status.HTTP_403_FORBIDDEN,
                detail="Cross-origin request rejected",
            )

    if sid is None:
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
        )

    try:
        session_uuid = UUID(sid)
    except ValueError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED, detail="Invalid session"
        ) from exc

    session = await get_valid_session(db, session_uuid)
    if session is None:
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail="Session expired or invalid",
        )

    if x_csrf_token is None or x_csrf_token != session.csrf_token:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="CSRF token missing or invalid",
        )

    return session


async def _require_session(
    sid: Annotated[str | None, Cookie(alias="sid")] = None,
    db: AsyncSession = Depends(get_db),
) -> Session:
    """Validate session cookie only (for read-only authenticated endpoints)."""
    if sid is None:
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
        )

    try:
        session_uuid = UUID(sid)
    except ValueError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED, detail="Invalid session"
        ) from exc

    session = await get_valid_session(db, session_uuid)
    if session is None:
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail="Session expired or invalid",
        )

    return session


async def _require_read_auth(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    sid: Annotated[str | None, Cookie(alias="sid")] = None,
    db: AsyncSession = Depends(get_db),
) -> ReadPrincipal:
    """Accept either a session cookie or a Bearer API token for read routes.

    Token path: only GET/HEAD methods; only routes in the allow-list.
    Non-safe method with a token → 403.
    Route not in allow-list → 403.
    Invalid/expired/revoked token → 401.
    """
    if authorization is not None:
        lower = authorization.lower()
        if lower.startswith("bearer "):
            plaintext = authorization[7:]
            if request.method not in ("GET", "HEAD"):
                raise HTTPException(
                    status_code=http_status.HTTP_403_FORBIDDEN,
                    detail="API tokens are read-only; use a session for mutations",
                )
            if not _is_token_allowed_path(request.url.path):
                raise HTTPException(
                    status_code=http_status.HTTP_403_FORBIDDEN,
                    detail="Route not accessible with an API token",
                )
            try:
                token = await verify_bearer_token(db, plaintext)
            except ThrottledError:
                raise HTTPException(
                    status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many failed requests for this token",
                ) from None
            if token is None:
                raise HTTPException(
                    status_code=http_status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid, expired, or revoked token",
                )
            return TokenContext(token_id=token.id, redact_addresses=token.redact_addresses)

    # Fall through to session-cookie auth.
    if sid is None:
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
        )

    try:
        session_uuid = UUID(sid)
    except ValueError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED, detail="Invalid session"
        ) from exc

    session = await get_valid_session(db, session_uuid)
    if session is None:
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail="Session expired or invalid",
        )
    return session


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/setup/status", response_model=SetupStatusResponse)
async def get_setup_status(db: AsyncSession = Depends(get_db)) -> SetupStatusResponse:
    setup_done = await is_setup(db)
    return SetupStatusResponse(setup_required=not setup_done)


@router.post("/setup", response_model=TokenResponse, status_code=http_status.HTTP_201_CREATED)
async def post_setup(
    body: PasswordBody,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    try:
        session_id, csrf_token = await setup_owner(db, body.password)
    except AlreadySetupError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Owner already configured",
        ) from exc
    _set_session_cookie(response, session_id)
    return TokenResponse(csrf_token=csrf_token)


@router.post("/auth/login", response_model=TokenResponse)
async def post_login(
    body: LoginBody,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    try:
        session_id, csrf_token = await login(db, body.password)
    except ThrottledError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed login attempts",
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc
    except (NotSetupError, AuthenticationError) as exc:
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        ) from exc
    _set_session_cookie(response, session_id)
    return TokenResponse(csrf_token=csrf_token)


@router.get("/auth/session", response_model=SessionResponse)
async def get_session(
    session: Annotated[Session, Depends(_require_session)],
) -> SessionResponse:
    expires_iso = session.expires_at.astimezone(UTC).isoformat()
    return SessionResponse(
        authenticated=True,
        expires_at=expires_iso,
        csrf_token=session.csrf_token,
    )


@router.post("/auth/logout", status_code=http_status.HTTP_204_NO_CONTENT)
async def post_logout(
    response: Response,
    session: Annotated[Session, Depends(_require_csrf)],
    db: AsyncSession = Depends(get_db),
) -> None:
    await revoke_session(db, session.id)
    _clear_session_cookie(response)


@router.api_route(
    "/auth/password", methods=["PUT", "PATCH"], status_code=http_status.HTTP_204_NO_CONTENT
)
async def patch_password(
    body: ChangePasswordBody,
    response: Response,
    session: Annotated[Session, Depends(_require_csrf)],
    db: AsyncSession = Depends(get_db),
) -> None:
    try:
        await change_password(db, session.id, body.current_password, body.new_password)
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="Current password is incorrect",
        ) from exc
    _clear_session_cookie(response)
