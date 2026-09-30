"""FastAPI routes for integration settings (AUD-290 / AUD-288).

GET  /api/v1/integrations               → IntegrationsResponse
PUT  /api/v1/integrations/rpc           → IntegrationEntry
PUT  /api/v1/integrations/quotes        → IntegrationEntry
POST /api/v1/integrations/{kind}/validate → JobRef
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import urlparse

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from audr.api.auth import _require_csrf, _require_session
from audr.auth.models import Session
from audr.db import get_db
from audr.jobs.store import JobKind, enqueue_job
from audr.providers.rpc_targets import RpcUrlError, validate_rpc_url_async
from audr.settings.integrations import RevisionConflictError, get_integration, upsert_integration
from audr.settings.quotes import save_coingecko_credentials

router = APIRouter(prefix="/api/v1")

# ---------------------------------------------------------------------------
# Internal constants — frontend kind ↔ DB kind mapping
# ---------------------------------------------------------------------------

_FE_TO_DB: dict[str, str] = {
    "rpc": "rpc",
    "quotes": "coingecko",
}

_DB_TO_FE: dict[str, str] = {v: k for k, v in _FE_TO_DB.items()}

_KIND_TO_JOB: dict[str, JobKind] = {
    "rpc": JobKind.VALIDATE_RPC,
    "quotes": JobKind.VALIDATE_QUOTES,
}

# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class IntegrationHealth(BaseModel):
    status: str  # ok | error | unvalidated | validating
    last_checked_at: str | None = None
    error_message: str | None = None


class IntegrationEntry(BaseModel):
    kind: str
    configured: bool
    enabled: bool
    provider: str | None = None
    host_label: str | None = None
    revision: str
    health: IntegrationHealth


class IntegrationsResponse(BaseModel):
    items: list[IntegrationEntry]
    request_id: str
    generated_at: str


class UpdateRpcInput(BaseModel):
    revision: str
    url: str
    headers: dict | None = None
    allow_private_host: bool = False


class UpdateQuotesInput(BaseModel):
    revision: str
    provider: str
    api_key: str | None = None


class JobRef(BaseModel):
    run_id: str
    coalesced: bool


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _get_health(db: AsyncSession, kind: str) -> IntegrationHealth:
    """Derive health from the most recent validation job run for this kind."""
    job_kind = _KIND_TO_JOB.get(kind)
    if job_kind is None:
        return IntegrationHealth(status="unvalidated")

    result = await db.execute(
        sa.text(
            """
            SELECT status, completed_at, error
            FROM job_run
            WHERE kind = :kind
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"kind": str(job_kind)},
    )
    row = result.first()
    if row is None:
        return IntegrationHealth(status="unvalidated")

    status_db, completed_at, error = row
    if status_db in ("pending", "in_progress"):
        return IntegrationHealth(status="validating", last_checked_at=None)
    if status_db == "completed":
        return IntegrationHealth(
            status="ok",
            last_checked_at=completed_at.isoformat() if completed_at else None,
        )
    # failed / cancelled
    return IntegrationHealth(
        status="error",
        last_checked_at=completed_at.isoformat() if completed_at else None,
        error_message=error,
    )


def _host_label(url: str | None) -> str | None:
    if not url:
        return None
    try:
        return urlparse(url).hostname
    except Exception:
        return None


async def _build_entry(db: AsyncSession, fe_kind: str, db_kind: str) -> IntegrationEntry:
    row = await get_integration(db, kind=db_kind, decrypt_fields=False)
    health = await _get_health(db, fe_kind)

    if row is None:
        return IntegrationEntry(
            kind=fe_kind,
            configured=False,
            enabled=False,
            provider=None,
            host_label=None,
            revision="0",
            health=health,
        )

    # For quotes, derive provider label from db_kind; for rpc, provider is null.
    provider: str | None = "coingecko" if db_kind == "coingecko" else None

    # Decrypt to get URL (for host_label on rpc) — only for rpc kind.
    host: str | None = None
    if fe_kind == "rpc":
        decrypted = await get_integration(db, kind=db_kind, decrypt_fields=True)
        host = _host_label(decrypted.url if decrypted else None)

    return IntegrationEntry(
        kind=fe_kind,
        configured=True,
        enabled=True,
        provider=provider,
        host_label=host,
        revision=str(row.revision),
        health=health,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/integrations", response_model=IntegrationsResponse)
async def get_integrations(
    _session: Annotated[Session, Depends(_require_session)],
    db: AsyncSession = Depends(get_db),
) -> IntegrationsResponse:
    items = [
        await _build_entry(db, "rpc", "rpc"),
        await _build_entry(db, "quotes", "coingecko"),
    ]
    now = datetime.now(tz=UTC)
    return IntegrationsResponse(
        items=items,
        request_id=str(uuid.uuid4()),
        generated_at=now.isoformat(),
    )


@router.put("/integrations/rpc", response_model=IntegrationEntry)
async def put_integration_rpc(
    body: UpdateRpcInput,
    _session: Annotated[Session, Depends(_require_csrf)],
    db: AsyncSession = Depends(get_db),
) -> IntegrationEntry:
    try:
        validated_url = await validate_rpc_url_async(
            body.url, allow_private_hosts=body.allow_private_host
        )
    except RpcUrlError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    try:
        expected_rev = int(body.revision) if body.revision else None
    except ValueError:
        expected_rev = None

    try:
        await upsert_integration(
            db,
            kind="rpc",
            url=validated_url,
            allow_private_host=body.allow_private_host,
            expected_revision=expected_rev,
        )
    except RevisionConflictError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="revision conflict",
        ) from exc

    await db.commit()
    return await _build_entry(db, "rpc", "rpc")


@router.put("/integrations/quotes", response_model=IntegrationEntry)
async def put_integration_quotes(
    body: UpdateQuotesInput,
    _session: Annotated[Session, Depends(_require_csrf)],
    db: AsyncSession = Depends(get_db),
) -> IntegrationEntry:
    if body.provider != "coingecko":
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unsupported provider: {body.provider!r}",
        )

    try:
        expected_rev = int(body.revision) if body.revision else None
    except ValueError:
        expected_rev = None

    try:
        await save_coingecko_credentials(
            db,
            api_key=body.api_key or "",
            expected_revision=expected_rev,
        )
    except RevisionConflictError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="revision conflict",
        ) from exc

    await db.commit()
    return await _build_entry(db, "quotes", "coingecko")


@router.post("/integrations/{kind}/validate", response_model=JobRef)
async def post_integration_validate(
    kind: str,
    _session: Annotated[Session, Depends(_require_csrf)],
    db: AsyncSession = Depends(get_db),
) -> JobRef:
    if kind not in _KIND_TO_JOB:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"unknown integration kind: {kind!r}",
        )
    job_kind = _KIND_TO_JOB[kind]

    # Coalesce if a run is already queued or running — the worker will pick up
    # the existing request; no need to stack another.
    existing = await db.execute(
        sa.text(
            "SELECT id FROM job_run WHERE kind = :kind"
            " AND status IN ('pending', 'in_progress')"
            " ORDER BY created_at DESC LIMIT 1"
        ),
        {"kind": str(job_kind)},
    )
    ex_row = existing.first()
    if ex_row is not None:
        return JobRef(run_id=str(ex_row[0]), coalesced=True)

    # Enqueue a pending request; the on-demand validation worker claims and
    # executes it on its next poll (AUD-313).
    run_id = await enqueue_job(db, kind=job_kind)
    await db.commit()
    return JobRef(run_id=str(run_id), coalesced=False)
