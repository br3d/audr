"""Integration tests for provider-data purge functionality.

Covers:
  - Preview: preview_purge returns correct non-negative counts without mutating data.
  - Authentication: execute_purge rejects wrong passwords with AuthenticationError.
  - Monetary data removal: quote_observation rows for the purged provider are deleted.
  - On-chain data preservation: balance_observation and wallet rows are never touched.
  - Job fencing: QUOTE_REFRESH claims return None after the coingecko integration
    is purged.
  - Integration removal: get_integration returns None after purge.
  - Count accuracy: preview_purge reports the exact number of seeded observations.

Requires (via fixtures in tests/conftest.py):
  - TEST_DATABASE_URL pointing at a test PostgreSQL instance.
  - Migrations applied through 004 (quote_observation, balance_observation tables).

Marker: integration
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audr.auth.service import AuthenticationError
from audr.jobs.store import JobKind, claim_job
from audr.operations.purge import execute_purge, preview_purge
from audr.settings.integrations import get_integration

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PURGE_PASSWORD = "correct-horse-battery-staple-purge-99"

# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


async def _insert_wallet(
    session: AsyncSession,
    address: str = "0x" + "1" * 40,
) -> uuid.UUID:
    """Insert a wallet row and return its UUID."""
    wallet_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO wallet (id, address, label, status)"
            " VALUES (:id, :addr, '', 'active')"
        ),
        {"id": str(wallet_id), "addr": address.lower()},
    )
    return wallet_id


async def _insert_asset(
    session: AsyncSession,
    *,
    token_address: str | None = None,
    symbol: str = "TKN",
) -> uuid.UUID:
    """Insert an asset row and return its UUID."""
    if token_address is None:
        token_address = "0x" + uuid.uuid4().hex[:40]
    asset_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO asset (id, token_address, symbol, name, decimals, source)"
            " VALUES (:id, :addr, :sym, :sym, 18, 'manual')"
        ),
        {"id": str(asset_id), "addr": token_address.lower(), "sym": symbol},
    )
    return asset_id


async def _insert_balance(
    session: AsyncSession,
    *,
    wallet_id: uuid.UUID,
    asset_id: uuid.UUID,
    block_number: int = 100,
) -> uuid.UUID:
    """Insert a balance_observation row and return its UUID."""
    obs_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO balance_observation"
            " (id, wallet_id, asset_id, raw_amount, block_number, observed_at)"
            " VALUES (:id, :wallet, :asset, 1000000000000000000, :block, now())"
        ),
        {
            "id": str(obs_id),
            "wallet": str(wallet_id),
            "asset": str(asset_id),
            "block": block_number,
        },
    )
    return obs_id


async def _insert_coingecko_quote(
    session: AsyncSession,
    *,
    asset_id: uuid.UUID,
    price_usd: Decimal = Decimal("1.0"),
) -> uuid.UUID:
    """Insert one coingecko quote_set + quote_observation; return observation UUID."""
    qset_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO quote_set (id, provider, fetched_at, status)"
            " VALUES (:id, 'coingecko', now(), 'complete')"
        ),
        {"id": str(qset_id)},
    )
    obs_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO quote_observation (id, quote_set_id, asset_id, price_usd)"
            " VALUES (:id, :qset, :asset, :price)"
        ),
        {
            "id": str(obs_id),
            "qset": str(qset_id),
            "asset": str(asset_id),
            "price": str(price_usd),
        },
    )
    return obs_id


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _clean_owner_table(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Truncate owner-related tables before each test to guarantee a clean start.

    Uses a committed session (not the rollback-wrapped db_session) so that the
    DELETE is visible to any subsequent committed transactions.
    """
    async with db_session_factory() as session:
        async with session.begin():
            await session.execute(text("DELETE FROM login_attempt"))
            await session.execute(text("DELETE FROM session"))
            await session.execute(text("DELETE FROM owner"))
    yield


@pytest.fixture()
async def owner_password(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> str:
    """Commit a singleton owner so execute_purge can authenticate callers.

    Returns the plaintext password to pass as the ``password`` argument.
    The owner row is committed (not inside the rollback-wrapped session) so it
    is visible to the test's db_session under READ COMMITTED isolation.
    """
    from audr.auth.service import setup_owner

    async with db_session_factory() as session:
        await setup_owner(session, _PURGE_PASSWORD)

    return _PURGE_PASSWORD


# ---------------------------------------------------------------------------
# Tests: preview
# ---------------------------------------------------------------------------


async def test_purge_preview_shows_counts(db_session: AsyncSession) -> None:
    """preview_purge returns a dict with non-negative integer counts."""
    result = await preview_purge(db_session, kind="rpc")

    assert isinstance(result["quote_observation_count"], int)
    assert isinstance(result["valuation_line_count"], int)
    assert isinstance(result["integration_count"], int)
    assert result["quote_observation_count"] >= 0
    assert result["valuation_line_count"] >= 0
    assert result["integration_count"] >= 0


async def test_purge_preview_does_not_delete(db_session: AsyncSession) -> None:
    """preview_purge must not remove any rows from the database."""
    wallet_id = await _insert_wallet(db_session)
    await db_session.flush()

    await preview_purge(db_session, kind="rpc")

    row = (
        await db_session.execute(
            text("SELECT id FROM wallet WHERE id = :id"),
            {"id": str(wallet_id)},
        )
    ).first()
    assert row is not None, "wallet row must survive a preview_purge call"


# ---------------------------------------------------------------------------
# Tests: authentication
# ---------------------------------------------------------------------------


async def test_purge_requires_password_confirmation(
    db_session: AsyncSession,
    owner_password: str,
) -> None:
    """execute_purge with a wrong password raises AuthenticationError."""
    with pytest.raises(AuthenticationError):
        await execute_purge(db_session, kind="coingecko", password="definitely-wrong")


# ---------------------------------------------------------------------------
# Tests: data deletion
# ---------------------------------------------------------------------------


async def test_purge_deletes_monetary_data(
    db_session: AsyncSession,
    owner_password: str,
) -> None:
    """execute_purge removes all coingecko quote_observation rows."""
    asset_id = await _insert_asset(db_session)
    await _insert_coingecko_quote(db_session, asset_id=asset_id)
    await db_session.flush()

    count_before = (
        await db_session.execute(
            text(
                "SELECT COUNT(*) FROM quote_observation qo"
                " JOIN quote_set qs ON qs.id = qo.quote_set_id"
                " WHERE qs.provider = 'coingecko'"
            )
        )
    ).scalar()
    assert count_before == 1

    await execute_purge(db_session, kind="coingecko", password=owner_password)

    count_after = (
        await db_session.execute(
            text(
                "SELECT COUNT(*) FROM quote_observation qo"
                " JOIN quote_set qs ON qs.id = qo.quote_set_id"
                " WHERE qs.provider = 'coingecko'"
            )
        )
    ).scalar()
    assert count_after == 0


async def test_purge_preserves_balance_observations(
    db_session: AsyncSession,
    owner_password: str,
) -> None:
    """execute_purge must not delete balance_observation rows (on-chain data)."""
    wallet_id = await _insert_wallet(db_session)
    asset_id = await _insert_asset(db_session)
    obs_id = await _insert_balance(db_session, wallet_id=wallet_id, asset_id=asset_id)
    await db_session.flush()

    await execute_purge(db_session, kind="coingecko", password=owner_password)

    row = (
        await db_session.execute(
            text("SELECT id FROM balance_observation WHERE id = :id"),
            {"id": str(obs_id)},
        )
    ).first()
    assert row is not None, "balance_observation must survive a coingecko purge"


async def test_purge_preserves_wallets(
    db_session: AsyncSession,
    owner_password: str,
) -> None:
    """execute_purge must not delete wallet rows."""
    wallet_id = await _insert_wallet(db_session)
    await db_session.flush()

    await execute_purge(db_session, kind="coingecko", password=owner_password)

    row = (
        await db_session.execute(
            text("SELECT id FROM wallet WHERE id = :id"),
            {"id": str(wallet_id)},
        )
    ).first()
    assert row is not None, "wallet must survive a coingecko purge"


# ---------------------------------------------------------------------------
# Tests: job fencing and integration state
# ---------------------------------------------------------------------------


async def test_purge_job_fencing_prevents_new_jobs(
    db_session: AsyncSession,
    owner_password: str,
) -> None:
    """QUOTE_REFRESH claims return None after the coingecko integration is purged."""
    await execute_purge(db_session, kind="coingecko", password=owner_password)
    await db_session.flush()

    run_id = await claim_job(db_session, kind=JobKind.QUOTE_REFRESH, max_retries=3)
    assert run_id is None, (
        "QUOTE_REFRESH must not be claimable after the coingecko integration is purged"
    )


async def test_purge_disables_integration(
    db_session: AsyncSession,
    owner_password: str,
) -> None:
    """After purge, get_integration returns None for the affected provider kind."""
    await execute_purge(db_session, kind="coingecko", password=owner_password)

    result = await get_integration(db_session, kind="coingecko")
    assert result is None, "integration row must be absent after purge"


# ---------------------------------------------------------------------------
# Tests: on-chain record retention
# ---------------------------------------------------------------------------


async def test_purge_retained_chain_records(
    db_session: AsyncSession,
    owner_password: str,
) -> None:
    """A balance_observation with a specific block_number survives a coingecko purge."""
    wallet_id = await _insert_wallet(db_session, "0x" + "f" * 40)
    asset_id = await _insert_asset(db_session, token_address="0x" + "e" * 40)
    block_number = 19_000_000
    await _insert_balance(
        db_session,
        wallet_id=wallet_id,
        asset_id=asset_id,
        block_number=block_number,
    )
    await db_session.flush()

    await execute_purge(db_session, kind="coingecko", password=owner_password)

    row = (
        await db_session.execute(
            text(
                "SELECT block_number FROM balance_observation"
                " WHERE wallet_id = :w AND asset_id = :a AND block_number = :b"
            ),
            {"w": str(wallet_id), "a": str(asset_id), "b": block_number},
        )
    ).first()
    assert row is not None, "balance_observation must survive a coingecko purge"
    assert row[0] == block_number


# ---------------------------------------------------------------------------
# Tests: preview count accuracy
# ---------------------------------------------------------------------------


async def test_purge_preview_coingecko_shows_correct_quote_count(
    db_session: AsyncSession,
) -> None:
    """preview_purge for coingecko reports quote_observation_count == 3 after seeding 3."""
    for i in range(3):
        # Each asset gets a distinct token address; each gets its own quote_set row.
        token_address = f"0x{'%040x' % (i + 1)}"
        asset_id = await _insert_asset(
            db_session, token_address=token_address, symbol=f"TKN{i}"
        )
        await _insert_coingecko_quote(db_session, asset_id=asset_id)
    await db_session.flush()

    result = await preview_purge(db_session, kind="coingecko")
    assert result["quote_observation_count"] == 3
