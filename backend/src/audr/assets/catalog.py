"""Catalog import and upgrade service (T034 / T035 / US1).

The catalog is a vendored snapshot of the Uniswap default token list
(Ethereum mainnet, chainId=1), committed into this repository rather than
fetched from GitHub at worker startup. A self-hosted product must not depend
on the availability of a specific upstream Git commit every time the worker
boots (AUD-357): the previously pinned commit stopped resolving and silently
left discovery with zero candidates.

Vendored source:
  Repository: https://github.com/Uniswap/default-token-list
  File:       src/tokens/mainnet.json
  Snapshot:   backend/src/audr/assets/data/uniswap_mainnet_tokenlist.json
"""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from audr.assets.models import CatalogEntry, CatalogVersion
from audr.operations.status import CatalogStatus, ComponentStatus

CATALOG_CHAIN_ID = 1  # Ethereum mainnet only

_VENDORED_CATALOG_PATH = (
    importlib.resources.files("audr.assets") / "data" / "uniswap_mainnet_tokenlist.json"
)


class CatalogImportError(Exception):
    """Raised when the catalog cannot be read or parsed."""


async def import_catalog(
    session: AsyncSession,
    *,
    path: Path = _VENDORED_CATALOG_PATH,
    chain_id: int = CATALOG_CHAIN_ID,
) -> CatalogVersion:
    """Import the vendored token catalog into catalog_version + catalog_entry.

    Idempotent: if this catalog snapshot (identified by a content hash of
    *path*) is already imported, returns the existing version without
    re-reading entries.
    """
    from sqlalchemy import select

    entries, version_id = _load_entries(path, chain_id=chain_id)

    existing = await session.execute(
        select(CatalogVersion).where(CatalogVersion.commit_hash == version_id)
    )
    row = existing.scalar_one_or_none()
    if row is not None:
        return row

    version = CatalogVersion(
        id=uuid.uuid4(),
        commit_hash=version_id,
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


async def get_catalog_status(session: AsyncSession) -> CatalogStatus:
    """Report whether a usable catalog has ever been imported (AUD-357).

    DEGRADED (zero entries) means ERC-20 discovery cannot find any
    candidates — this must stay visible on /health/ready and /api/v1/status
    rather than only appearing as a worker-log WARNING.
    """
    version = await get_latest_catalog_version(session)
    entry_count = version.entry_count if version is not None else 0
    status = ComponentStatus.OK if entry_count > 0 else ComponentStatus.DEGRADED
    return CatalogStatus(entry_count=entry_count, status=status)


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


def _load_entries(path: Path, *, chain_id: int) -> tuple[list[dict[str, Any]], str]:
    """Read and parse the catalog JSON file at *path*.

    Returns the chain-filtered token entries plus an opaque content-hash
    version id used for idempotency (stored in catalog_version.commit_hash).
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CatalogImportError(f"Failed to read catalog file {path}: {exc}") from exc

    try:
        data = json.loads(raw)
    except Exception as exc:
        raise CatalogImportError("Catalog file is not valid JSON") from exc

    # Support both array format and EIP-1616 object format {"tokens": [...]}
    if isinstance(data, list):
        tokens = data
    elif isinstance(data, dict) and "tokens" in data:
        tokens = data["tokens"]
    else:
        raise CatalogImportError("Unrecognised catalog format")

    entries = [t for t in tokens if t.get("chainId") == chain_id]
    version_id = f"vendored:{hashlib.sha256(raw).hexdigest()[:16]}"
    return entries, version_id
