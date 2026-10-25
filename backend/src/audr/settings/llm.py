"""Anthropic API key storage (AUD-658). Same encrypted integration store
`settings/quotes.py` uses for the CoinGecko key — `'anthropic'` is declared
in `ck_integration_kind` by migration 0043.

Never logs or exposes the key.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from audr.settings.integrations import (
    IntegrationRead,
    RevisionConflictError,
    get_integration,
    upsert_integration,
)

__all__ = [
    "RevisionConflictError",
    "get_anthropic_api_key",
    "get_anthropic_credentials",
    "save_anthropic_credentials",
]

_KIND = "anthropic"


async def get_anthropic_credentials(session: AsyncSession) -> IntegrationRead | None:
    """Return the Anthropic integration metadata (without decrypting)."""
    return await get_integration(session, kind=_KIND, decrypt_fields=False)


async def get_anthropic_api_key(session: AsyncSession) -> str | None:
    """Return the plaintext Anthropic API key, or None if not configured."""
    record = await get_integration(session, kind=_KIND, decrypt_fields=True)
    if record is None:
        return None
    return record.api_key


async def save_anthropic_credentials(
    session: AsyncSession,
    *,
    api_key: str,
    expected_revision: int | None = None,
) -> IntegrationRead:
    """Persist the Anthropic API key (encrypted).

    Raises RevisionConflictError if expected_revision does not match the
    current stored revision (optimistic concurrency control).
    """
    return await upsert_integration(
        session,
        kind=_KIND,
        api_key=api_key,
        expected_revision=expected_revision,
    )
