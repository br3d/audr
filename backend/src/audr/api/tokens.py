"""API token management routes (AUD-629).

Create / list / revoke read-only Bearer tokens.  All three routes require an
owner session (+ CSRF for mutations) — tokens themselves cannot reach these
routes (``/api/v1/tokens*`` is not in the token allow-list).
"""

from __future__ import annotations

import uuid
from datetime import UTC
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from audr.api.auth import _require_csrf, _require_session
from audr.auth.models import ApiToken, Session
from audr.auth.service import list_tokens, mint_token, revoke_token
from audr.db import get_db

router = APIRouter(prefix="/api/v1")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class TokenCreateInput(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    expires_days: int = Field(default=90, ge=1, le=3650)
    redact_addresses: bool = False


class TokenOut(BaseModel):
    id: str
    name: str
    scopes: str
    expires_at: str | None
    created_at: str
    last_used_at: str | None
    redact_addresses: bool


class TokenCreatedOut(TokenOut):
    """Returned only on creation — includes the plaintext token (shown once)."""

    token: str


class TokensListResponse(BaseModel):
    items: list[TokenOut]


def _token_to_out(t: ApiToken) -> TokenOut:
    return TokenOut(
        id=str(t.id),
        name=t.name,
        scopes=t.scopes,
        expires_at=t.expires_at.astimezone(UTC).isoformat() if t.expires_at else None,
        created_at=t.created_at.astimezone(UTC).isoformat(),
        last_used_at=t.last_used_at.astimezone(UTC).isoformat() if t.last_used_at else None,
        redact_addresses=t.redact_addresses,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/tokens", response_model=TokensListResponse)
async def list_api_tokens(
    _session: Annotated[Session, Depends(_require_session)],
    db: AsyncSession = Depends(get_db),
) -> TokensListResponse:
    """Return all active (non-revoked) tokens."""
    tokens = await list_tokens(db)
    return TokensListResponse(items=[_token_to_out(t) for t in tokens])


@router.post("/tokens", response_model=TokenCreatedOut, status_code=http_status.HTTP_201_CREATED)
async def create_api_token(
    body: TokenCreateInput,
    _session: Annotated[Session, Depends(_require_csrf)],
    db: AsyncSession = Depends(get_db),
) -> TokenCreatedOut:
    """Mint a new read-only API token.

    The plaintext value is returned exactly once and not stored; the caller
    must copy it immediately — there is no way to retrieve it later.
    """
    plaintext, token = await mint_token(
        db,
        name=body.name,
        expires_days=body.expires_days,
        redact_addresses=body.redact_addresses,
    )
    out = _token_to_out(token)
    return TokenCreatedOut(**out.model_dump(), token=plaintext)


@router.delete(
    "/tokens/{token_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
)
async def revoke_api_token(
    token_id: uuid.UUID,
    _session: Annotated[Session, Depends(_require_csrf)],
    db: AsyncSession = Depends(get_db),
) -> None:
    """Revoke a token by id.  Revoked tokens are immediately rejected."""
    found = await revoke_token(db, token_id)
    if not found:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="token not found",
        )
