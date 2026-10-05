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
from audr.providers.rpc_defaults import DEFAULT_PUBLIC_RPC_URLS
from audr.providers.rpc_targets import RpcUrlError, validate_rpc_url_async
from audr.settings.integrations import RevisionConflictError, get_integration, upsert_integration
from audr.settings.quotes import get_coingecko_api_key, save_coingecko_credentials

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

# The quote providers the backend can actually drive, in the order the owner
# should see them (AUD-440). `requires_api_key` is what makes the keyless
# default explainable in the UI instead of the owner having to guess a name
# into a free-text field; `coinmarketcap` is selected by storing no key at
# all, which is exactly what jobs.quotes.get_active_quote_provider reads.
_QUOTE_PROVIDER_IDS = ("coinmarketcap", "coingecko")

# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class IntegrationHealth(BaseModel):
    status: str  # ok | error | unvalidated | validating
    last_checked_at: str | None = None
    error_message: str | None = None


class ProviderOption(BaseModel):
    """One selectable provider, with what it costs the owner to use it."""

    id: str
    label: str
    requires_api_key: bool
    note: str


class IntegrationEntry(BaseModel):
    kind: str
    configured: bool
    enabled: bool
    provider: str | None = None
    host_label: str | None = None
    revision: str
    health: IntegrationHealth
    # What this integration is actually using right now, configured or not —
    # a fresh install has working keyless defaults, and the UI used to show
    # it as simply unavailable (AUD-440).
    effective_source: str | None = None
    using_default: bool = False
    options: list[ProviderOption] = []


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


_QUOTE_PROVIDER_OPTIONS: list[ProviderOption] = [
    ProviderOption(
        id="coinmarketcap",
        label="CoinMarketCap (public endpoints)",
        requires_api_key=False,
        note=(
            "Used by default and needs no account or API key. Rate-limited for "
            "anonymous callers, and only prices assets listed on CoinMarketCap."
        ),
    ),
    ProviderOption(
        id="coingecko",
        label="CoinGecko (Demo API)",
        requires_api_key=True,
        note=(
            "Needs a free CoinGecko Demo API key. Prices tokens by contract "
            "address, so it covers more ERC-20 tokens than the default."
        ),
    ),
]

_QUOTE_PROVIDER_LABELS: dict[str, str] = {o.id: o.label for o in _QUOTE_PROVIDER_OPTIONS}

# The keyless public endpoint a fresh install reads the chain through — the
# head of the fallback list jobs actually use (providers.rpc_targets).
_DEFAULT_RPC_HOST = urlparse(DEFAULT_PUBLIC_RPC_URLS[0]).hostname


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


async def _build_rpc_entry(db: AsyncSession) -> IntegrationEntry:
    """Describe the RPC integration, including the default used when unset.

    An unconfigured RPC is not an unusable one: jobs fall back to the keyless
    public endpoints, so this reports that endpoint as the effective source
    with `using_default` set, rather than reporting nothing (AUD-440).
    """
    row = await get_integration(db, kind="rpc", decrypt_fields=False)
    health = await _get_health(db, "rpc")

    if row is None:
        return IntegrationEntry(
            kind="rpc",
            configured=False,
            enabled=True,
            revision="0",
            health=health,
            effective_source=_DEFAULT_RPC_HOST,
            using_default=True,
        )

    decrypted = await get_integration(db, kind="rpc", decrypt_fields=True)
    host = _host_label(decrypted.url if decrypted else None)
    return IntegrationEntry(
        kind="rpc",
        configured=True,
        enabled=True,
        host_label=host,
        revision=str(row.revision),
        health=health,
        effective_source=host,
        using_default=False,
    )


async def _build_quotes_entry(db: AsyncSession) -> IntegrationEntry:
    """Describe the quote provider actually in use, plus the selectable set.

    The active provider is derived the same way the quote job derives it — a
    stored CoinGecko key means CoinGecko, anything else means the keyless
    CoinMarketCap default — so the UI cannot claim prices are unavailable
    while the worker is happily pricing the portfolio (AUD-440).

    A row with a blank key counts as *not* configured: blanking the key is how
    the owner reverts to the default, and the row lingers to carry the
    revision.
    """
    row = await get_integration(db, kind="coingecko", decrypt_fields=False)
    health = await _get_health(db, "quotes")
    api_key = await get_coingecko_api_key(db)

    provider = "coingecko" if api_key else "coinmarketcap"
    return IntegrationEntry(
        kind="quotes",
        configured=bool(api_key),
        enabled=True,
        provider=provider,
        revision=str(row.revision) if row is not None else "0",
        health=health,
        effective_source=_QUOTE_PROVIDER_LABELS[provider],
        using_default=provider == "coinmarketcap",
        options=_QUOTE_PROVIDER_OPTIONS,
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
        await _build_rpc_entry(db),
        await _build_quotes_entry(db),
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
    return await _build_rpc_entry(db)


@router.put("/integrations/quotes", response_model=IntegrationEntry)
async def put_integration_quotes(
    body: UpdateQuotesInput,
    _session: Annotated[Session, Depends(_require_csrf)],
    db: AsyncSession = Depends(get_db),
) -> IntegrationEntry:
    if body.provider not in _QUOTE_PROVIDER_IDS:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unsupported provider: {body.provider!r}",
        )

    # Selecting the keyless default means storing no key — that is the single
    # piece of state the quote job reads to pick a provider, so it must be
    # cleared here rather than left behind pointing at CoinGecko (AUD-440).
    api_key = (body.api_key or "").strip() if body.provider == "coingecko" else ""
    if body.provider == "coingecko" and not api_key:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="coingecko requires an API key; select coinmarketcap to use the keyless default",
        )

    try:
        expected_rev = int(body.revision) if body.revision else None
    except ValueError:
        expected_rev = None

    try:
        await save_coingecko_credentials(
            db,
            api_key=api_key,
            expected_revision=expected_rev,
        )
    except RevisionConflictError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="revision conflict",
        ) from exc

    await db.commit()
    return await _build_quotes_entry(db)


@router.post(
    "/integrations/{kind}/validate",
    response_model=JobRef,
    status_code=http_status.HTTP_202_ACCEPTED,
)
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
