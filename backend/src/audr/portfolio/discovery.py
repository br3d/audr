"""Token discovery — chunked, resumable scanning (T039 / US1).

Discovers which ERC-20 tokens a wallet holds by:
1. Loading catalog entries for the current catalog version.
2. Including any user-added manual token addresses.
3. Deduplicating candidates by lowercase address.

Discovery does NOT probe balances — it only builds the candidate list.
Checkpointing allows interrupted runs to resume from where they left off.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from audr.assets.models import CatalogEntry, CatalogVersion


@dataclass
class DiscoveryCandidate:
    token_address: str
    source: str  # "catalog" | "manual"


@dataclass
class DiscoveryResult:
    candidates: list[DiscoveryCandidate] = field(default_factory=list)
    checkpoint: dict[str, Any] | None = None


async def discover_tokens(
    session: AsyncSession,
    *,
    wallet_address: str,
    use_catalog: bool,
    manual_addresses: list[str],
    checkpoint: dict[str, Any] | None = None,
) -> DiscoveryResult:
    """Build the discovery candidate list for *wallet_address*.

    If *checkpoint* is provided, resumes from the saved state (currently:
    skip catalog addresses already processed).
    """
    seen: set[str] = set()
    candidates: list[DiscoveryCandidate] = []

    processed_from_checkpoint: set[str] = set()
    if checkpoint and "processed" in checkpoint:
        processed_from_checkpoint = set(checkpoint["processed"])

    if use_catalog:
        catalog_entries = await _get_catalog_entries(session)
        for entry in catalog_entries:
            addr = entry.token_address.lower()
            if addr in seen or addr in processed_from_checkpoint:
                continue
            seen.add(addr)
            candidates.append(DiscoveryCandidate(token_address=addr, source="catalog"))

    for raw_addr in manual_addresses:
        addr = raw_addr.lower()
        if addr in seen:
            continue
        seen.add(addr)
        candidates.append(DiscoveryCandidate(token_address=addr, source="manual"))

    return DiscoveryResult(
        candidates=candidates,
        checkpoint={"processed": list(seen)},
    )


async def save_discovery_checkpoint(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    checkpoint: dict[str, Any],
) -> None:
    """Persist *checkpoint* on the job_run row identified by *run_id*."""
    await session.execute(
        sa.text(
            "UPDATE job_run SET checkpoint = CAST(:checkpoint AS jsonb) WHERE id = :id"
        ),
        {"id": str(run_id), "checkpoint": _json_dumps(checkpoint)},
    )
    await session.flush()


async def get_discovery_checkpoint(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
) -> dict[str, Any] | None:
    """Return the checkpoint stored on the job_run row, or None."""
    result = await session.execute(
        sa.text("SELECT checkpoint FROM job_run WHERE id = :id"),
        {"id": str(run_id)},
    )
    row = result.first()
    if row is None or row[0] is None:
        return None
    return dict(row[0])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _get_catalog_entries(session: AsyncSession) -> list[CatalogEntry]:
    """Return entries from the most recently imported catalog version."""
    version_result = await session.execute(
        sa.select(CatalogVersion).order_by(CatalogVersion.imported_at.desc()).limit(1)
    )
    version = version_result.scalar_one_or_none()
    if version is None:
        return []
    entries_result = await session.execute(
        sa.select(CatalogEntry).where(CatalogEntry.version_id == version.id)
    )
    return list(entries_result.scalars())


async def _wallet_id_for_address(
    session: AsyncSession, address: str
) -> uuid.UUID | None:
    result = await session.execute(
        sa.text("SELECT id FROM wallet WHERE address = :addr"),
        {"addr": address.lower()},
    )
    row = result.first()
    return uuid.UUID(str(row[0])) if row else None


def _json_dumps(obj: dict[str, Any]) -> str:
    import json

    return json.dumps(obj)
