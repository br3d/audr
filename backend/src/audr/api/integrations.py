"""FastAPI routes for integration settings (AUD-290 / AUD-288).

GET  /api/v1/integrations               → IntegrationsResponse
PUT  /api/v1/integrations/rpc           → IntegrationEntry
PUT  /api/v1/integrations/quotes        → IntegrationEntry
PUT  /api/v1/integrations/llm           → IntegrationEntry (AUD-658/932, universal LLM provider)
POST /api/v1/integrations/{kind}/validate → JobRef (rpc | quotes | llm)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated
from urllib.parse import urlparse

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from audr.api.auth import _require_csrf, _require_session
from audr.auth.models import Session
from audr.config import get_settings
from audr.db import get_db
from audr.jobs.store import JobKind, enqueue_job
from audr.providers.rpc_defaults import DEFAULT_PUBLIC_RPC_URLS
from audr.providers.rpc_targets import RpcUrlError, validate_rpc_url_async
from audr.settings.integrations import RevisionConflictError, get_integration, upsert_integration
from audr.settings.llm import (
    PROVIDER_ANTHROPIC,
    PROVIDER_OPENAI_COMPATIBLE,
    LlmConfig,
    load_llm_config,
    save_llm_config,
)
from audr.settings.quotes import get_coingecko_api_key, save_coingecko_credentials

router = APIRouter(prefix="/api/v1")

# ---------------------------------------------------------------------------
# Internal constants — frontend kind ↔ DB kind mapping
# ---------------------------------------------------------------------------

_FE_TO_DB: dict[str, str] = {
    "rpc": "rpc",
    "quotes": "coingecko",
    "llm": "anthropic",
}

_DB_TO_FE: dict[str, str] = {v: k for k, v in _FE_TO_DB.items()}

_KIND_TO_JOB: dict[str, JobKind] = {
    "rpc": JobKind.VALIDATE_RPC,
    "quotes": JobKind.VALIDATE_QUOTES,
    "llm": JobKind.VALIDATE_LLM,
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
    # LLM presets (AUD-932): what selecting the preset pre-fills in the form.
    # `provider` is the wire protocol to store (`anthropic` | `openai_compatible`).
    provider: str | None = None
    base_url: str | None = None
    default_model: str | None = None
    # True for LAN/localhost presets: saving needs `allow_private_host`.
    private_host: bool = False


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
    # LLM only (AUD-932). The key itself is never returned.
    base_url: str | None = None
    model: str | None = None
    allow_private_host: bool | None = None
    price_input_per_mtok: str | None = None
    price_output_per_mtok: str | None = None


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


class UpdateLlmInput(BaseModel):
    revision: str
    provider: str = PROVIDER_ANTHROPIC
    base_url: str | None = None
    # Empty/absent on update = keep the stored key (AUD-932).
    api_key: str | None = None
    model: str | None = None
    allow_private_host: bool = False
    price_input_per_mtok: Decimal | None = None
    price_output_per_mtok: Decimal | None = None


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


def _llm(
    id_: str,
    label: str,
    base_url: str | None,
    model: str | None,
    note: str,
    *,
    key: bool = True,
    provider: str = PROVIDER_OPENAI_COMPATIBLE,
    private: bool = False,
) -> ProviderOption:
    return ProviderOption(
        id=id_,
        label=label,
        requires_api_key=key,
        note=note,
        provider=provider,
        base_url=base_url,
        default_model=model,
        private_host=private,
    )


# Selectable LLM presets (AUD-932). Everything but `anthropic` speaks the OpenAI
# `/chat/completions` protocol; a preset only pre-fills the form — what is stored
# is provider + base_url + model, so "custom" and the presets are one mechanism.
_LLM_PROVIDER_OPTIONS: list[ProviderOption] = [
    _llm(
        "anthropic",
        "Anthropic (Claude)",
        None,
        "claude-haiku-4-5",
        "Native Messages API. Known Claude prices drive the per-run budget.",
        provider=PROVIDER_ANTHROPIC,
    ),
    _llm(
        "openai", "OpenAI", "https://api.openai.com/v1", "gpt-4o-mini", "Needs an OpenAI API key."
    ),
    _llm(
        "openrouter",
        "OpenRouter",
        "https://openrouter.ai/api/v1",
        "openai/gpt-4o-mini",
        "One key for many hosted models; model ids look like vendor/model.",
    ),
    _llm(
        "groq",
        "Groq",
        "https://api.groq.com/openai/v1",
        "llama-3.1-8b-instant",
        "Fast hosted open models; free tier available.",
    ),
    _llm(
        "deepseek",
        "DeepSeek",
        "https://api.deepseek.com/v1",
        "deepseek-chat",
        "Needs a DeepSeek API key.",
    ),
    _llm(
        "mistral",
        "Mistral",
        "https://api.mistral.ai/v1",
        "mistral-small-latest",
        "Needs a Mistral API key.",
    ),
    _llm(
        "gemini",
        "Google Gemini (OpenAI-compatible)",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "gemini-2.0-flash",
        "Gemini's OpenAI-compatible endpoint; needs a Gemini API key.",
    ),
    _llm(
        "ollama",
        "Ollama (local)",
        "http://localhost:11434/v1",
        "llama3.1",
        "Runs on your own machine or LAN host; no key. Replace localhost with the host "
        "running Ollama.",
        key=False,
        private=True,
    ),
    _llm(
        "lmstudio",
        "LM Studio (local)",
        "http://localhost:1234/v1",
        None,
        "LM Studio's local server; no key. Set the model to the one loaded.",
        key=False,
        private=True,
    ),
    _llm(
        "custom",
        "Custom (OpenAI-compatible)",
        None,
        None,
        "Any server that implements POST {base_url}/chat/completions. Key optional.",
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


async def _build_llm_entry(db: AsyncSession) -> IntegrationEntry:
    """Describe the LLM provider used by the news-analysis gate (AUD-658, AUD-932).

    Configuring a provider does not turn the gate on by itself — that is the
    separate global flag at GET/PATCH /settings/news-analysis. This entry only
    reports what is stored. The API key is never included.
    """
    row = await get_integration(db, kind="anthropic", decrypt_fields=False)
    health = await _get_health(db, "llm")
    config = await load_llm_config(db)
    configured = config is not None and (
        bool(config.api_key) or config.provider == PROVIDER_OPENAI_COMPATIBLE
    )
    label = None
    if config is not None and configured:
        label = (
            "Anthropic"
            if config.provider == PROVIDER_ANTHROPIC
            else (_host_label(config.base_url) or "OpenAI-compatible")
        )
    return IntegrationEntry(
        kind="llm",
        configured=configured,
        enabled=True,
        provider=config.provider if config else PROVIDER_ANTHROPIC,
        revision=str(row.revision) if row is not None else "0",
        health=health,
        effective_source=label,
        using_default=False,
        options=_LLM_PROVIDER_OPTIONS,
        base_url=config.base_url if config else None,
        model=config.model if config else None,
        allow_private_host=config.allow_private_host if config else None,
        host_label=_host_label(config.base_url) if config else None,
        price_input_per_mtok=(
            str(config.price_input_per_mtok)
            if config and config.price_input_per_mtok is not None
            else None
        ),
        price_output_per_mtok=(
            str(config.price_output_per_mtok)
            if config and config.price_output_per_mtok is not None
            else None
        ),
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
        await _build_llm_entry(db),
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


@router.put("/integrations/llm", response_model=IntegrationEntry)
async def put_integration_llm(
    body: UpdateLlmInput,
    _session: Annotated[Session, Depends(_require_csrf)],
    db: AsyncSession = Depends(get_db),
) -> IntegrationEntry:
    if body.provider not in (PROVIDER_ANTHROPIC, PROVIDER_OPENAI_COMPATIBLE):
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="provider must be 'anthropic' or 'openai_compatible'",
        )
    for price in (body.price_input_per_mtok, body.price_output_per_mtok):
        if price is not None and (price < 0 or not price.is_finite()):
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="prices must be non-negative numbers",
            )

    stored = await load_llm_config(db)
    base_url: str | None = None
    allow_private = False
    if body.provider == PROVIDER_OPENAI_COMPATIBLE:
        raw_url = (body.base_url or "").strip()
        if not raw_url:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="base_url is required for an OpenAI-compatible provider",
            )
        allow_private = body.allow_private_host
        try:
            base_url = await validate_rpc_url_async(raw_url, allow_private_hosts=allow_private)
        except RpcUrlError as exc:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"base_url rejected: {exc}",
            ) from exc

    # Same provider *and* same endpoint → "empty = keep" the stored secrets.
    # A changed provider or host never inherits the old key: it would be sent
    # to a server the owner never typed it in for.
    same_target = (
        stored is not None
        and stored.provider == body.provider
        and (body.provider == PROVIDER_ANTHROPIC or stored.base_url == base_url)
    )
    api_key = (body.api_key or "").strip() or (stored.api_key if same_target and stored else None)
    if body.provider == PROVIDER_ANTHROPIC and not api_key:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="api_key must not be empty",
        )
    model = (
        (body.model or "").strip()
        or (stored.model if same_target and stored else "")
        or get_settings().llm_news_model
    )
    if body.provider == PROVIDER_OPENAI_COMPATIBLE and not (
        (body.model or "").strip() or (same_target and stored)
    ):
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="model is required for an OpenAI-compatible provider",
        )
    price_in = body.price_input_per_mtok
    price_out = body.price_output_per_mtok
    if same_target and stored:
        price_in = price_in if price_in is not None else stored.price_input_per_mtok
        price_out = price_out if price_out is not None else stored.price_output_per_mtok

    try:
        expected_rev = int(body.revision) if body.revision else None
    except ValueError:
        expected_rev = None

    try:
        await save_llm_config(
            db,
            config=LlmConfig(
                provider=body.provider,
                base_url=base_url,
                api_key=api_key,
                model=model,
                price_input_per_mtok=price_in,
                price_output_per_mtok=price_out,
                allow_private_host=allow_private,
            ),
            expected_revision=expected_rev,
        )
    except RevisionConflictError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="revision conflict",
        ) from exc

    await db.commit()
    return await _build_llm_entry(db)


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
