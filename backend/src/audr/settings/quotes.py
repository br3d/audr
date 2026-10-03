"""CoinGecko quote-provider credential storage (T053 / US2 / AUD-66).

Thin wrapper around the encrypted integration store.  The 'coingecko' kind
is already declared in the integration table CHECK constraint (migration 002).

Never logs or exposes the API key.
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
    "get_coingecko_credentials",
    "get_coingecko_api_key",
    "save_coingecko_credentials",
]

_KIND = "coingecko"


async def get_coingecko_credentials(
    session: AsyncSession,
) -> IntegrationRead | None:
    """Return the CoinGecko integration metadata (without decrypting)."""
    return await get_integration(session, kind=_KIND, decrypt_fields=False)


async def get_coingecko_api_key(
    session: AsyncSession,
) -> str | None:
    """Return the plaintext CoinGecko API key, or None if not configured."""
    record = await get_integration(session, kind=_KIND, decrypt_fields=True)
    if record is None:
        return None
    return record.api_key


async def save_coingecko_credentials(
    session: AsyncSession,
    *,
    api_key: str,
    expected_revision: int | None = None,
) -> IntegrationRead:
    """Persist the CoinGecko API key (encrypted).

    Raises RevisionConflictError if expected_revision does not match the
    current stored revision (optimistic concurrency control).
    """
    return await upsert_integration(
        session,
        kind=_KIND,
        api_key=api_key,
        expected_revision=expected_revision,
    )
