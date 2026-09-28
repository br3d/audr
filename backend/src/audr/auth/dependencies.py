"""FastAPI dependencies for authentication (T028 / US1)."""

from __future__ import annotations

from fastapi import Cookie, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from audr.auth.service import SessionRow, get_session
from audr.db import get_db

_SESSION_COOKIE = "audr_session"


async def require_session(
    request: Request,
    token: str | None = Cookie(default=None, alias=_SESSION_COOKIE),
    session: AsyncSession = Depends(get_db),
) -> SessionRow:
    """FastAPI dependency: require an authenticated session.

    Raises 401 if the token is absent or expired.
    """
    if token is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    row = await get_session(session, token=token)
    if row is None:
        raise HTTPException(status_code=401, detail="session expired or invalid")
    return row


def _origin_ok(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin is None:
        return True  # same-origin requests from non-browser clients are ok
    host = request.headers.get("host", "")
    return origin.rstrip("/").endswith(host)
