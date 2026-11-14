"""LLM provider storage (AUD-658, AUD-932).

One universal integration: provider type + base URL + API key + model (+ optional
prices). It lives in the same encrypted integration row the Anthropic-only
version used — DB kind `'anthropic'`, declared in `ck_integration_kind` by
migration 0043 — so an existing key keeps working with zero owner action: a row
with no stored `provider` *is* an Anthropic row. The kind name is historical;
renaming it would be a constraint migration for no behavioural gain.

Never logs or exposes the key.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from sqlalchemy.ext.asyncio import AsyncSession

from audr.config import get_settings
from audr.providers.rpc_targets import validate_rpc_url_async
from audr.settings.integrations import (
    IntegrationRead,
    RevisionConflictError,
    get_integration,
    upsert_integration,
)

__all__ = [
    "PROVIDER_ANTHROPIC",
    "PROVIDER_OPENAI_COMPATIBLE",
    "LlmConfig",
    "RevisionConflictError",
    "get_anthropic_api_key",
    "get_anthropic_credentials",
    "load_llm_config",
    "load_validated_llm_config",
    "save_anthropic_credentials",
    "save_llm_config",
]

_KIND = "anthropic"

PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OPENAI_COMPATIBLE = "openai_compatible"
PROVIDERS = (PROVIDER_ANTHROPIC, PROVIDER_OPENAI_COMPATIBLE)


@dataclass(frozen=True)
class LlmConfig:
    provider: str
    base_url: str | None
    api_key: str | None
    model: str
    price_input_per_mtok: Decimal | None
    price_output_per_mtok: Decimal | None
    allow_private_host: bool = False


def _dec(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


async def get_anthropic_credentials(session: AsyncSession) -> IntegrationRead | None:
    """Return the LLM integration metadata (without decrypting)."""
    return await get_integration(session, kind=_KIND, decrypt_fields=False)


async def load_llm_config(session: AsyncSession) -> LlmConfig | None:
    """The decrypted provider config, or None if nothing is stored.

    Model falls back to env `llm_news_model` when the row predates AUD-932.
    """
    record = await get_integration(session, kind=_KIND, decrypt_fields=True)
    if record is None:
        return None
    extra = record.extra
    provider = extra.get("provider") or PROVIDER_ANTHROPIC
    return LlmConfig(
        provider=provider,
        base_url=record.url,
        api_key=record.api_key or None,
        model=extra.get("model") or get_settings().llm_news_model,
        price_input_per_mtok=_dec(extra.get("price_input_per_mtok")),
        price_output_per_mtok=_dec(extra.get("price_output_per_mtok")),
        allow_private_host=record.allow_private_host,
    )


async def get_anthropic_api_key(session: AsyncSession) -> str | None:
    """Return the plaintext API key, or None if not configured."""
    config = await load_llm_config(session)
    return config.api_key if config else None


async def save_llm_config(
    session: AsyncSession,
    *,
    config: LlmConfig,
    expected_revision: int | None = None,
) -> IntegrationRead:
    """Persist the whole provider config (encrypted).

    Raises RevisionConflictError if expected_revision does not match.
    """
    extra: dict[str, str] = {"provider": config.provider, "model": config.model}
    if config.price_input_per_mtok is not None:
        extra["price_input_per_mtok"] = str(config.price_input_per_mtok)
    if config.price_output_per_mtok is not None:
        extra["price_output_per_mtok"] = str(config.price_output_per_mtok)
    return await upsert_integration(
        session,
        kind=_KIND,
        url=config.base_url,
        api_key=config.api_key,
        allow_private_host=config.allow_private_host,
        extra=extra,
        expected_revision=expected_revision,
    )


async def save_anthropic_credentials(
    session: AsyncSession,
    *,
    api_key: str,
    expected_revision: int | None = None,
) -> IntegrationRead:
    """Persist an Anthropic key with default model (kept for callers/tests)."""
    return await save_llm_config(
        session,
        config=LlmConfig(
            provider=PROVIDER_ANTHROPIC,
            base_url=None,
            api_key=api_key,
            model=get_settings().llm_news_model,
            price_input_per_mtok=None,
            price_output_per_mtok=None,
        ),
        expected_revision=expected_revision,
    )


async def load_validated_llm_config(session: AsyncSession) -> LlmConfig | None:
    """`load_llm_config`, re-checking an OpenAI-compatible base URL against the
    private-host policy it was saved with, right before use (DNS-rebinding
    window — same reasoning as `get_validated_rpc_url`).

    Raises RpcUrlError if the stored URL no longer passes.
    """
    config = await load_llm_config(session)
    if config is not None and config.provider == PROVIDER_OPENAI_COMPATIBLE and config.base_url:
        await validate_rpc_url_async(config.base_url, allow_private_hosts=config.allow_private_host)
    return config
