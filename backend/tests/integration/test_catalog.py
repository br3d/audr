"""Catalog import tests (AUD-357).

The catalog was previously fetched over HTTP from a pinned Uniswap GitHub
commit, which stopped resolving (404) and left discovery with zero
candidates — every heartbeat silently logged a WARNING and moved on, so
ERC-20 holdings could never be discovered. The fix vendors the token list
into the repository and makes a missing/empty catalog an explicit,
externally visible DEGRADED status rather than a log line nobody reads.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audr.assets.catalog import (
    CatalogImportError,
    get_catalog_status,
    get_latest_catalog_version,
    import_catalog,
)
from audr.operations.status import ComponentStatus

BUTERIN_ADDRESS = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
# Known-real mainnet tokens that must be present in the vendored snapshot.
DAI_ADDRESS = "0x6b175474e89094c44da98b954eedeac495271d0f"
USDC_ADDRESS = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"


@pytest.mark.integration
async def test_import_catalog_reads_vendored_tokens(db_session: AsyncSession) -> None:
    """The vendored snapshot imports real mainnet tokens, not a stub row."""
    version = await import_catalog(db_session)

    assert version.entry_count > 300, "vendored Uniswap mainnet list should have hundreds of tokens"

    rows = await db_session.execute(
        sa.text("SELECT token_address FROM catalog_entry WHERE version_id = :vid"),
        {"vid": str(version.id)},
    )
    addresses = {r[0] for r in rows}
    assert DAI_ADDRESS in addresses
    assert USDC_ADDRESS in addresses


@pytest.mark.integration
async def test_import_catalog_is_idempotent(db_session: AsyncSession) -> None:
    """Re-importing the same vendored snapshot returns the existing version
    and does not duplicate catalog_entry rows."""
    first = await import_catalog(db_session)
    second = await import_catalog(db_session)

    assert first.id == second.id

    count = await db_session.execute(sa.text("SELECT COUNT(*) FROM catalog_version"))
    assert count.scalar() == 1


@pytest.mark.integration
async def test_import_catalog_raises_explicitly_when_file_missing(
    db_session: AsyncSession,
) -> None:
    """A missing/unreadable catalog file must fail loudly (CatalogImportError),
    never silently succeed with zero entries."""
    with pytest.raises(CatalogImportError):
        await import_catalog(db_session, path=Path("/nonexistent/catalog.json"))

    assert await get_latest_catalog_version(db_session) is None


@pytest.mark.integration
async def test_catalog_status_degraded_without_import(db_session: AsyncSession) -> None:
    """No catalog ever imported → DEGRADED with zero entries, not "unknown"."""
    status = await get_catalog_status(db_session)

    assert status.status == ComponentStatus.DEGRADED
    assert status.entry_count == 0


@pytest.mark.integration
async def test_catalog_status_ok_after_import(db_session: AsyncSession) -> None:
    await import_catalog(db_session)

    status = await get_catalog_status(db_session)

    assert status.status == ComponentStatus.OK
    assert status.entry_count > 300


@pytest.mark.integration
async def test_health_ready_surfaces_catalog_degradation(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """_collect_status() must report catalog=degraded when nothing has been
    imported, and catalog=ok once the vendored snapshot lands — this is the
    externally-visible signal that replaces the old silent WARNING log.
    """
    from unittest.mock import patch

    from audr.api.health import _collect_status

    with patch("audr.db._get_session_factory", return_value=db_session_factory):
        before = await _collect_status()
    assert before.catalog.status == ComponentStatus.DEGRADED

    try:
        async with db_session_factory() as session:
            await import_catalog(session)
            await session.commit()

        with patch("audr.db._get_session_factory", return_value=db_session_factory):
            after = await _collect_status()
        assert after.catalog.status == ComponentStatus.OK
        assert after.catalog.entry_count > 300
    finally:
        async with db_session_factory() as session:
            await session.execute(sa.text("DELETE FROM catalog_entry"))
            await session.execute(sa.text("DELETE FROM catalog_version"))
            await session.commit()


async def _insert_wallet(session: AsyncSession, address: str) -> uuid.UUID:
    wallet_id = uuid.uuid4()
    await session.execute(
        sa.text(
            "INSERT INTO wallet (id, address, label, status) VALUES (:id, :addr, '', 'active')"
        ),
        {"id": str(wallet_id), "addr": address.lower()},
    )
    await session.flush()
    return wallet_id


@pytest.mark.integration
async def test_discovery_finds_candidates_from_real_vendored_catalog(
    db_session: AsyncSession,
) -> None:
    """End-to-end acceptance check (AUD-357): once the vendored catalog is
    imported, discovery on a wallet yields candidates>0 and persists real
    monitored_pair rows — not the pre-fix candidates=0.
    """
    from audr.portfolio.discovery import discover_tokens, persist_discovery_candidates

    await import_catalog(db_session)
    await _insert_wallet(db_session, BUTERIN_ADDRESS)

    result = await discover_tokens(
        db_session,
        wallet_address=BUTERIN_ADDRESS,
        use_catalog=True,
        manual_addresses=[],
    )
    assert len(result.candidates) > 300

    new_pairs = await persist_discovery_candidates(
        db_session,
        wallet_address=BUTERIN_ADDRESS,
        candidates=result.candidates,
    )
    assert new_pairs > 300

    pair_count = await db_session.execute(
        sa.text(
            """
            SELECT COUNT(*) FROM monitored_pair mp
            JOIN wallet w ON w.id = mp.wallet_id
            WHERE w.address = :addr
            """
        ),
        {"addr": BUTERIN_ADDRESS},
    )
    assert pair_count.scalar() > 300
