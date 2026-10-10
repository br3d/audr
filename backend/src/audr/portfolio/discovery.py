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
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from audr.assets.models import CatalogEntry, CatalogVersion
from audr.operations.init_key import get_master_key
from audr.wallets.service import compute_address_bidx


@dataclass
class DiscoveryCandidate:
    token_address: str
    source: str  # "catalog" | "manual"
    symbol: str = "UNKNOWN"
    name: str = "Unknown Token"
    decimals: int = 18


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
            candidates.append(
                DiscoveryCandidate(
                    token_address=addr,
                    source="catalog",
                    symbol=entry.symbol,
                    name=entry.name,
                    decimals=entry.decimals,
                )
            )

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


async def persist_discovery_candidates(
    session: AsyncSession,
    *,
    wallet_address: str,
    candidates: list[DiscoveryCandidate],
) -> int:
    """Idempotently upsert asset + monitored_pair rows for each candidate.

    Returns the count of newly created monitored_pair rows (existing pairs are
    counted as zero).  The wallet row must already exist.
    """
    if not candidates:
        return 0

    addr = wallet_address.lower()
    key = await get_master_key(session)
    bidx = compute_address_bidx(addr, key)
    wallet_result = await session.execute(
        sa.text("SELECT id FROM wallet WHERE address_bidx = :bidx"),
        {"bidx": bidx},
    )
    wallet_row = wallet_result.first()
    if wallet_row is None:
        return 0
    wallet_id = str(wallet_row[0])

    new_pairs = 0
    for candidate in candidates:
        token_addr = candidate.token_address.lower()

        # Upsert asset — keep existing row unchanged if already present.
        await session.execute(
            sa.text(
                """
                INSERT INTO asset (id, token_address, symbol, name, decimals, source)
                VALUES (:id, :token_address, :symbol, :name, :decimals, :source)
                ON CONFLICT (token_address) DO NOTHING
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "token_address": token_addr,
                "symbol": candidate.symbol,
                "name": candidate.name,
                "decimals": candidate.decimals,
                "source": candidate.source,
            },
        )

        asset_result = await session.execute(
            sa.text("SELECT id FROM asset WHERE token_address = :addr"),
            {"addr": token_addr},
        )
        asset_row = asset_result.first()
        if asset_row is None:
            continue
        asset_id = str(asset_row[0])

        # Upsert monitored_pair — unique on (wallet_id, asset_id).
        result = await session.execute(
            sa.text(
                """
                INSERT INTO monitored_pair (id, wallet_id, asset_id)
                VALUES (:id, :wallet_id, :asset_id)
                ON CONFLICT (wallet_id, asset_id) DO NOTHING
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "wallet_id": wallet_id,
                "asset_id": asset_id,
            },
        )
        if result.rowcount and result.rowcount > 0:
            new_pairs += 1

    await session.flush()
    return new_pairs


async def save_discovery_checkpoint(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    checkpoint: dict[str, Any],
) -> None:
    """Persist *checkpoint* on the job_run row identified by *run_id*."""
    await session.execute(
        sa.text("UPDATE job_run SET checkpoint = CAST(:checkpoint AS jsonb) WHERE id = :id"),
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


def _json_dumps(obj: dict[str, Any]) -> str:
    import json

    return json.dumps(obj)
