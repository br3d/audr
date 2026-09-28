"""Catalog import and upgrade service (T034 / T035 / US1).

The catalog is a pinned commit of the Uniswap token list (EIP-1616 format).
Only Ethereum mainnet tokens (chainId=1) are imported.

Pinned commit:
  Repository: https://github.com/Uniswap/default-token-list
  Commit:     ba9f85db4bc18c8f69ce72b2327c5cdfe8a02e53
  File:       src/tokens/mainnet.json
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from audr.assets.models import CatalogEntry, CatalogVersion

# Pinned Ethereum-only catalog (reviewed 2026-09-27)
PINNED_COMMIT = "ba9f85db4bc18c8f69ce72b2327c5cdfe8a02e53"
CATALOG_CHAIN_ID = 1  # Ethereum mainnet only

_CATALOG_URL = (
    "https://raw.githubusercontent.com/Uniswap/default-token-list"
    f"/{PINNED_COMMIT}/src/tokens/mainnet.json"
)


class CatalogImportError(Exception):
    """Raised when the catalog cannot be fetched or parsed."""


async def import_catalog(
    session: AsyncSession,
    *,
    url: str = _CATALOG_URL,
    commit_hash: str = PINNED_COMMIT,
    chain_id: int = CATALOG_CHAIN_ID,
    http_client: httpx.AsyncClient | None = None,
) -> CatalogVersion:
    """Fetch and import a token catalog into catalog_version + catalog_entry tables.

    Idempotent: if *commit_hash* is already imported, returns the existing version.
    """
    from sqlalchemy import select

    existing = await session.execute(
        select(CatalogVersion).where(CatalogVersion.commit_hash == commit_hash)
    )
    row = existing.scalar_one_or_none()
    if row is not None:
        return row

    entries = await _fetch_entries(url, chain_id=chain_id, client=http_client)

    version = CatalogVersion(
        id=uuid.uuid4(),
        commit_hash=commit_hash,
        chain_id=chain_id,
        entry_count=len(entries),
    )
    session.add(version)
    await session.flush()

    for entry_data in entries:
        entry = CatalogEntry(
            id=uuid.uuid4(),
            version_id=version.id,
            token_address=entry_data["address"].lower(),
            symbol=entry_data["symbol"],
            name=entry_data["name"],
            decimals=int(entry_data["decimals"]),
        )
        session.add(entry)

    await session.flush()
    return version


async def get_latest_catalog_version(
    session: AsyncSession,
) -> CatalogVersion | None:
    """Return the most recently imported catalog version, or None."""
    from sqlalchemy import select

    result = await session.execute(
        select(CatalogVersion).order_by(CatalogVersion.imported_at.desc()).limit(1)
    )
    return result.scalar_one_or_none()


async def list_catalog_entries(
    session: AsyncSession,
    *,
    version_id: uuid.UUID,
) -> list[CatalogEntry]:
    """Return all catalog entries for *version_id*."""
    from sqlalchemy import select

    result = await session.execute(
        select(CatalogEntry).where(CatalogEntry.version_id == version_id)
    )
    return list(result.scalars())


async def _fetch_entries(
    url: str,
    *,
    chain_id: int,
    client: httpx.AsyncClient | None,
) -> list[dict[str, Any]]:
    """Fetch token list JSON from *url* and filter to *chain_id*."""
    owned = client is None
    if owned:
        client = httpx.AsyncClient(follow_redirects=False, timeout=30.0)
    try:
        response = await client.get(url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise CatalogImportError(f"Failed to fetch catalog from {url}: {exc}") from exc
    finally:
        if owned:
            await client.aclose()

    try:
        data = response.json()
    except Exception as exc:
        raise CatalogImportError("Catalog response is not valid JSON") from exc

    # Support both array format and EIP-1616 object format {"tokens": [...]}
    if isinstance(data, list):
        tokens = data
    elif isinstance(data, dict) and "tokens" in data:
        tokens = data["tokens"]
    else:
        raise CatalogImportError("Unrecognised catalog format")

    return [t for t in tokens if t.get("chainId") == chain_id]
