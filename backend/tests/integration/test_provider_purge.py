"""Integration tests for provider-data purge functionality.

Covers:
  - Preview: preview_purge returns correct non-negative counts without mutating data.
  - Authentication: execute_purge rejects wrong passwords with AuthenticationError.
  - Monetary data removal: quote_observation rows for the purged provider are deleted.
  - On-chain data preservation: balance_observation and wallet rows are never touched.
  - Job fencing: QUOTE_REFRESH stays claimable after the coingecko integration
    is purged (AUD-358 default CoinMarketCap fallback).
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
import respx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audr.auth.service import AuthenticationError
from audr.jobs.quotes import handle_quote_refresh
from audr.jobs.store import JobKind, JobRunStatus, claim_job
from audr.operations.purge import execute_purge, preview_purge
from audr.settings.integrations import get_integration
from tests.helpers import wallet_address_columns

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
    cols = await wallet_address_columns(session, wallet_id, address)
    await session.execute(
        text(
            "INSERT INTO wallet (id, address_ciphertext, address_bidx, label_ciphertext, status)"
            " VALUES (:id, :addr_ct, :addr_bidx, '', 'active')"
        ),
        {
            "id": str(wallet_id),
            "addr_ct": cols["address_ciphertext"],
            "addr_bidx": cols["address_bidx"],
        },
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


async def test_purge_job_fencing_still_allows_claim_via_cmc_fallback(
    db_session: AsyncSession,
    owner_password: str,
) -> None:
    """QUOTE_REFRESH stays claimable after the coingecko integration is purged.

    Before AUD-358, QUOTE_REFRESH hard-required a live coingecko integration
    to be claimable at all, which is exactly why a fresh install (zero
    integrations configured) could never price anything out of the box.
    Purging the coingecko key now just falls back to the keyless
    CoinMarketCap default inside handle_quote_refresh — it must not also
    block the job from being claimed in the first place.
    """
    await execute_purge(db_session, kind="coingecko", password=owner_password)
    await db_session.flush()

    run_id = await claim_job(db_session, kind=JobKind.QUOTE_REFRESH, max_retries=3)
    assert run_id is not None, (
        "QUOTE_REFRESH must remain claimable via the CoinMarketCap fallback "
        "after the coingecko integration is purged"
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
        asset_id = await _insert_asset(db_session, token_address=token_address, symbol=f"TKN{i}")
        await _insert_coingecko_quote(db_session, asset_id=asset_id)
    await db_session.flush()

    result = await preview_purge(db_session, kind="coingecko")
    assert result["quote_observation_count"] == 3


# ---------------------------------------------------------------------------
# Helpers: valuation data
# ---------------------------------------------------------------------------


async def _insert_valuation_snapshot(
    session: AsyncSession,
    quality: str = "complete",
) -> uuid.UUID:
    snap_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO valuation_snapshot (id, snapshotted_at, quality, published_at, input_key)"
            " VALUES (:id, now(), :quality, now(), :input_key)"
        ),
        {"id": str(snap_id), "quality": quality, "input_key": str(snap_id)},
    )
    return snap_id


async def _insert_valuation_line(
    session: AsyncSession,
    *,
    snapshot_id: uuid.UUID,
    wallet_id: uuid.UUID,
    asset_id: uuid.UUID,
    price_usd: Decimal | None = Decimal("1.0"),
) -> uuid.UUID:
    line_id = uuid.uuid4()
    value_usd = price_usd  # simplified 1-unit holding
    await session.execute(
        text(
            "INSERT INTO valuation_line"
            " (id, snapshot_id, wallet_id, asset_id, raw_amount, block_number,"
            "  price_usd, value_usd)"
            " VALUES (:id, :snap, :wallet, :asset, 1000000000000000000, 100,"
            "         :price, :value)"
        ),
        {
            "id": str(line_id),
            "snap": str(snapshot_id),
            "wallet": str(wallet_id),
            "asset": str(asset_id),
            "price": str(price_usd) if price_usd is not None else None,
            "value": str(value_usd) if value_usd is not None else None,
        },
    )
    return line_id


# ---------------------------------------------------------------------------
# Tests: provider-scoped valuation counts
# ---------------------------------------------------------------------------


async def test_purge_preview_rpc_valuation_count_is_zero(
    db_session: AsyncSession,
) -> None:
    """preview_purge for rpc always returns valuation_line_count == 0."""
    wallet_id = await _insert_wallet(db_session)
    asset_id = await _insert_asset(db_session)
    snap_id = await _insert_valuation_snapshot(db_session)
    await _insert_valuation_line(
        db_session,
        snapshot_id=snap_id,
        wallet_id=wallet_id,
        asset_id=asset_id,
        price_usd=Decimal("100.0"),
    )
    await db_session.flush()

    result = await preview_purge(db_session, kind="rpc")
    assert result["valuation_line_count"] == 0
    assert result["quote_set_count"] == 0


async def test_purge_preview_coingecko_valuation_count_only_priced(
    db_session: AsyncSession,
) -> None:
    """preview_purge for coingecko counts only valuation_line rows with price_usd set."""
    wallet_id = await _insert_wallet(db_session)
    asset1 = await _insert_asset(db_session, symbol="PRICED1")
    asset2 = await _insert_asset(db_session, symbol="PRICED2")
    asset3 = await _insert_asset(db_session, symbol="STALE")

    snap_id = await _insert_valuation_snapshot(db_session, quality="partial")
    await _insert_valuation_line(
        db_session,
        snapshot_id=snap_id,
        wallet_id=wallet_id,
        asset_id=asset1,
        price_usd=Decimal("100.0"),
    )
    await _insert_valuation_line(
        db_session,
        snapshot_id=snap_id,
        wallet_id=wallet_id,
        asset_id=asset2,
        price_usd=Decimal("200.0"),
    )
    await _insert_valuation_line(
        db_session,
        snapshot_id=snap_id,
        wallet_id=wallet_id,
        asset_id=asset3,
        price_usd=None,  # stale / unpriced
    )
    await db_session.flush()

    result = await preview_purge(db_session, kind="coingecko")
    # Only the 2 priced lines should be counted, not the stale one.
    assert result["valuation_line_count"] == 2


async def test_purge_preview_coingecko_quote_set_count_accurate(
    db_session: AsyncSession,
) -> None:
    """preview_purge for coingecko reports the actual number of quote_set rows."""
    asset_id = await _insert_asset(db_session)
    for _ in range(3):
        await _insert_coingecko_quote(db_session, asset_id=asset_id)
    await db_session.flush()

    result = await preview_purge(db_session, kind="coingecko")
    # quote_set_count must reflect actual rows, not just 0 or 1.
    assert result["quote_set_count"] == 3


# ---------------------------------------------------------------------------
# Tests: valuation data deletion
# ---------------------------------------------------------------------------


async def test_purge_deletes_priced_valuation_lines(
    db_session: AsyncSession,
    owner_password: str,
) -> None:
    """execute_purge for coingecko deletes valuation_line rows that have price_usd set."""
    wallet_id = await _insert_wallet(db_session)
    asset_id = await _insert_asset(db_session)
    snap_id = await _insert_valuation_snapshot(db_session)
    line_id = await _insert_valuation_line(
        db_session,
        snapshot_id=snap_id,
        wallet_id=wallet_id,
        asset_id=asset_id,
        price_usd=Decimal("50.0"),
    )
    await db_session.flush()

    await execute_purge(db_session, kind="coingecko", password=owner_password)

    row = (
        await db_session.execute(
            text("SELECT id FROM valuation_line WHERE id = :id"), {"id": str(line_id)}
        )
    ).first()
    assert row is None, "priced valuation_line must be deleted by coingecko purge"


async def test_purge_preserves_unpriced_valuation_lines(
    db_session: AsyncSession,
    owner_password: str,
) -> None:
    """execute_purge for coingecko does NOT delete valuation_line rows with price_usd IS NULL."""
    wallet_id = await _insert_wallet(db_session)
    asset_id = await _insert_asset(db_session)
    snap_id = await _insert_valuation_snapshot(db_session, quality="stale")
    line_id = await _insert_valuation_line(
        db_session,
        snapshot_id=snap_id,
        wallet_id=wallet_id,
        asset_id=asset_id,
        price_usd=None,
    )
    await db_session.flush()

    await execute_purge(db_session, kind="coingecko", password=owner_password)

    row = (
        await db_session.execute(
            text("SELECT id FROM valuation_line WHERE id = :id"), {"id": str(line_id)}
        )
    ).first()
    assert row is not None, "unpriced valuation_line must NOT be deleted by coingecko purge"


# ---------------------------------------------------------------------------
# Tests: resurrection race (fencing)
# ---------------------------------------------------------------------------


async def test_quote_refresh_fenced_on_cancelled_run(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A quote_refresh run whose job_run is cancelled before commit must not persist data.

    This exercises the fencing check added to handle_quote_refresh: after the
    external API call returns, the handler re-reads the job_run status.  If the
    purge already cancelled it, no quote_set should be committed.

    get_coingecko_api_key is patched to return a fake key so the test does not
    depend on the key_state / encryption setup of the shared test database.
    """
    from unittest.mock import AsyncMock, patch

    import httpx

    # Use a unique wallet address to avoid collisions with the default fixtures.
    unique_addr = "0xfeed" + "0" * 36
    wallet_id: uuid.UUID
    asset_id: uuid.UUID
    run_id = uuid.uuid4()

    async with db_session_factory() as s:
        async with s.begin():
            wallet_id = await _insert_wallet(s, unique_addr)
            asset_id = await _insert_asset(s, token_address="0x" + "c" * 40, symbol="FENCE")
            await _insert_balance(s, wallet_id=wallet_id, asset_id=asset_id)

    async with db_session_factory() as s:
        async with s.begin():
            await s.execute(
                text(
                    "INSERT INTO job_run (id, kind, status, max_retries)"
                    " VALUES (:id, 'quote_refresh', 'cancelled', 3)"
                ),
                {"id": str(run_id)},
            )

    try:
        # Count quote_set rows before the call so we can assert no net addition.
        before_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM quote_set WHERE provider = 'coingecko'")
            )
        ).scalar()

        # Patch get_coingecko_api_key so the handler proceeds without needing real
        # encryption, and mock the HTTP call so it reaches the fencing check.
        with (
            patch(
                "audr.jobs.quotes.get_coingecko_api_key",
                new=AsyncMock(return_value="fake-key-for-fence-test"),
            ),
        ):
            with respx.mock(assert_all_called=False) as mock_router:
                # Mock all CoinGecko endpoints (simple/price and token_price/*).
                mock_router.get(url__regex=r"coingecko").mock(
                    return_value=httpx.Response(200, json={})
                )
                await handle_quote_refresh(db_session, run_id)

        after_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM quote_set WHERE provider = 'coingecko'")
            )
        ).scalar()

        assert after_count == before_count, (
            "fenced quote_refresh must not leave any new quote_set rows"
        )
    finally:
        # Always clean up committed rows, even if the assertion fails.
        async with db_session_factory() as s:
            async with s.begin():
                await s.execute(
                    text("DELETE FROM balance_observation WHERE wallet_id = :w"),
                    {"w": str(wallet_id)},
                )
                await s.execute(text("DELETE FROM wallet WHERE id = :w"), {"w": str(wallet_id)})
                await s.execute(text("DELETE FROM asset WHERE id = :a"), {"a": str(asset_id)})
                await s.execute(text("DELETE FROM job_run WHERE id = :id"), {"id": str(run_id)})


@pytest.mark.integration
async def test_quote_refresh_marks_empty_on_zero_observations(
    db_session: AsyncSession,
) -> None:
    """handle_quote_refresh marks the quote_set 'empty' when no usable prices are returned.

    Regression (AUD-273): zero-observation sets were previously marked 'complete', causing
    _get_latest_prices to shadow earlier sets that had valid prices.

    All rows are inserted within db_session (rolled-back transaction) so the fencing
    checks in handle_quote_refresh find the job_run and integration without needing
    separate committed sessions.
    """
    from unittest.mock import AsyncMock, patch

    import httpx

    run_id = uuid.uuid4()

    # Asset with a non-zero balance so _get_held_asset_addresses returns it.
    wallet_id = await _insert_wallet(db_session, "0xface" + "0" * 36)
    asset_id = await _insert_asset(db_session, token_address="0x" + "e1" * 20, symbol="ZEROPRICE")
    await _insert_balance(db_session, wallet_id=wallet_id, asset_id=asset_id)

    # Fencing check needs an in_progress job_run and a coingecko integration.
    await db_session.execute(
        text(
            "INSERT INTO job_run (id, kind, status, max_retries)"
            " VALUES (:id, 'quote_refresh', 'in_progress', 3)"
        ),
        {"id": str(run_id)},
    )
    await db_session.execute(
        text(
            "INSERT INTO integration (kind, encrypted_blob)"
            " VALUES ('coingecko', :blob) ON CONFLICT (kind) DO NOTHING"
        ),
        {"blob": b"\x00"},
    )
    await db_session.flush()

    with patch(
        "audr.jobs.quotes.get_coingecko_api_key",
        new=AsyncMock(return_value="fake-key"),
    ):
        with respx.mock(assert_all_called=False) as mock_router:
            # CoinGecko returns no prices for any of the held tokens.
            mock_router.get(url__regex=r"coingecko").mock(return_value=httpx.Response(200, json={}))
            await handle_quote_refresh(db_session, run_id)

    row = (
        await db_session.execute(
            text(
                "SELECT status FROM quote_set"
                " WHERE provider = 'coingecko'"
                " ORDER BY fetched_at DESC LIMIT 1"
            )
        )
    ).first()
    assert row is not None
    assert row[0] == "empty", f"expected 'empty' but got '{row[0]}'"


# ---------------------------------------------------------------------------
# Tests: provider failure must not be recorded as a successful run (AUD-318)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_quote_refresh_provider_error_raises_and_marks_quote_set_failed(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A CoinGecko provider error must propagate, not be swallowed.

    Before AUD-318, handle_quote_refresh marked the quote_set 'failed' but
    returned normally, so the worker recorded the job_run as 'completed' and
    advanced schedule.last_run_at — a provider outage looked like success.
    This test uses committed sessions (not the rollback-wrapped db_session
    fixture) because the fix commits the 'failed' quote_set write before
    raising, so the worker's separate fail_job() session can record the run
    as failed without depending on this transaction.
    """
    from unittest.mock import AsyncMock, patch

    import httpx

    from audr.providers.coingecko_demo import CoinGeckoError

    unique_addr = "0xdead" + "0" * 36
    wallet_id: uuid.UUID
    asset_id: uuid.UUID
    run_id = uuid.uuid4()

    async with db_session_factory() as s:
        async with s.begin():
            wallet_id = await _insert_wallet(s, unique_addr)
            asset_id = await _insert_asset(s, token_address="0x" + "d1" * 20, symbol="FAILPRICE")
            await _insert_balance(s, wallet_id=wallet_id, asset_id=asset_id)
            await s.execute(
                text(
                    "INSERT INTO job_run (id, kind, status, max_retries)"
                    " VALUES (:id, 'quote_refresh', 'in_progress', 3)"
                ),
                {"id": str(run_id)},
            )
            await s.execute(
                text(
                    "INSERT INTO integration (kind, encrypted_blob)"
                    " VALUES ('coingecko', :blob) ON CONFLICT (kind) DO NOTHING"
                ),
                {"blob": b"\x00"},
            )

    quote_set_id: uuid.UUID | None = None
    try:
        async with db_session_factory() as session:
            with patch(
                "audr.jobs.quotes.get_coingecko_api_key",
                new=AsyncMock(return_value="fake-key-for-failure-test"),
            ):
                with respx.mock(assert_all_called=False) as mock_router:
                    mock_router.get(url__regex=r"coingecko").mock(
                        return_value=httpx.Response(500, json={"error": "boom"})
                    )
                    with pytest.raises(CoinGeckoError):
                        await handle_quote_refresh(session, run_id)

        async with db_session_factory() as session:
            row = (
                await session.execute(
                    text("SELECT id, status FROM quote_set ORDER BY fetched_at DESC LIMIT 1")
                )
            ).first()
            assert row is not None
            quote_set_id = row[0]
            assert row[1] == "failed", (
                f"a provider error must leave the quote_set 'failed', not '{row[1]}'"
            )
    finally:
        async with db_session_factory() as s:
            async with s.begin():
                await s.execute(
                    text("DELETE FROM balance_observation WHERE wallet_id = :w"),
                    {"w": str(wallet_id)},
                )
                await s.execute(text("DELETE FROM wallet WHERE id = :w"), {"w": str(wallet_id)})
                await s.execute(text("DELETE FROM asset WHERE id = :a"), {"a": str(asset_id)})
                await s.execute(text("DELETE FROM job_run WHERE id = :id"), {"id": str(run_id)})
                if quote_set_id is not None:
                    await s.execute(
                        text("DELETE FROM quote_set WHERE id = :id"),
                        {"id": str(quote_set_id)},
                    )


@pytest.mark.integration
async def test_quote_refresh_provider_error_worker_records_failed_run(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """End-to-end: Worker.run_once marks the job_run 'failed', not 'completed',
    when the handler it dispatches raises on a provider error (AUD-318)."""
    from unittest.mock import AsyncMock, patch

    import httpx

    from audr.jobs.store import get_job_run
    from audr.jobs.worker import Worker

    unique_addr = "0xbeef" + "0" * 36
    wallet_id: uuid.UUID
    asset_id: uuid.UUID

    async with db_session_factory() as s:
        async with s.begin():
            wallet_id = await _insert_wallet(s, unique_addr)
            asset_id = await _insert_asset(s, token_address="0x" + "e2" * 20, symbol="WORKERFAIL")
            await _insert_balance(s, wallet_id=wallet_id, asset_id=asset_id)
            await s.execute(
                text(
                    "INSERT INTO integration (kind, encrypted_blob)"
                    " VALUES ('coingecko', :blob) ON CONFLICT (kind) DO NOTHING"
                ),
                {"blob": b"\x00"},
            )

    run_id: uuid.UUID | None = None
    quote_set_id: uuid.UUID | None = None
    try:
        worker = Worker(
            db_session_factory,
            kind=JobKind.QUOTE_REFRESH,
            handler=handle_quote_refresh,
        )
        with patch(
            "audr.jobs.quotes.get_coingecko_api_key",
            new=AsyncMock(return_value="fake-key-for-worker-failure-test"),
        ):
            with respx.mock(assert_all_called=False) as mock_router:
                mock_router.get(url__regex=r"coingecko").mock(
                    return_value=httpx.Response(500, json={"error": "boom"})
                )
                did_work = await worker.run_once()
        assert did_work is True

        async with db_session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT id FROM job_run WHERE kind = 'quote_refresh'"
                        " ORDER BY created_at DESC LIMIT 1"
                    )
                )
            ).first()
            assert row is not None
            run_id = row[0]

            run = await get_job_run(session, run_id=run_id)
            assert run is not None
            assert run.status == JobRunStatus.FAILED, (
                "a provider error must record the run as failed, not completed"
            )

            qs_row = (
                await session.execute(
                    text("SELECT id FROM quote_set ORDER BY fetched_at DESC LIMIT 1")
                )
            ).first()
            quote_set_id = qs_row[0] if qs_row else None
    finally:
        async with db_session_factory() as s:
            async with s.begin():
                await s.execute(
                    text("DELETE FROM balance_observation WHERE wallet_id = :w"),
                    {"w": str(wallet_id)},
                )
                await s.execute(text("DELETE FROM wallet WHERE id = :w"), {"w": str(wallet_id)})
                await s.execute(text("DELETE FROM asset WHERE id = :a"), {"a": str(asset_id)})
                if run_id is not None:
                    await s.execute(text("DELETE FROM job_run WHERE id = :id"), {"id": str(run_id)})
                if quote_set_id is not None:
                    await s.execute(
                        text("DELETE FROM quote_set WHERE id = :id"),
                        {"id": str(quote_set_id)},
                    )
