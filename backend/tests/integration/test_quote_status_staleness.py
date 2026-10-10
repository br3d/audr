"""/health/ready quote staleness signal (AUD-370).

A held asset can show a non-NULL price while quote_refresh has actually
been failing for hours (a sustained CoinMarketCap 429, for example) — the
last successful price lingers and the portfolio looks healthy even though
nothing has been refreshed. get_quote_status must report DEGRADED once the
last *complete* quote_set is older than 2x freshness_s, not just when every
price is NULL.

Marker: integration
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from audr.jobs.quotes import get_quote_status
from audr.operations.status import ComponentStatus

pytestmark = pytest.mark.integration

# get_quote_status resolves the active provider via get_coingecko_api_key,
# which reads the encrypted integrations table and needs a real master key —
# irrelevant to the staleness signal under test here, so it's patched to
# "no CoinGecko key configured" the same way test_quote_refresh_freshness.py
# does for handle_quote_refresh.
_no_coingecko_key = patch(
    "audr.jobs.quotes.get_coingecko_api_key", new=AsyncMock(return_value=None)
)


async def _insert_wallet_and_asset(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    wallet_id = uuid.uuid4()
    asset_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO wallet (id, address, label_ciphertext, status) VALUES (:id, :addr, '', 'active')"),
        {"id": str(wallet_id), "addr": "0x" + "1a" * 20},
    )
    await session.execute(
        text(
            """
            INSERT INTO asset (id, token_address, symbol, name, decimals, source)
            VALUES (:id, :addr, 'TKN', 'TKN', 18, 'manual')
            """
        ),
        {"id": str(asset_id), "addr": "0x" + "2b" * 20},
    )
    return wallet_id, asset_id


async def _insert_priced_snapshot(
    session: AsyncSession, *, wallet_id: uuid.UUID, asset_id: uuid.UUID
) -> None:
    snapshot_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO valuation_snapshot (id, quality, published_at, input_key)"
            " VALUES (:id, 'complete', now(), :input_key)"
        ),
        {"id": str(snapshot_id), "input_key": str(snapshot_id)},
    )
    await session.execute(
        text(
            """
            INSERT INTO valuation_line
              (id, snapshot_id, wallet_id, asset_id, raw_amount, block_number, price_usd, value_usd)
            VALUES
              (:id, :snap, :wallet, :asset, 1000000000000000000, 100, 5.00, 5.00)
            """
        ),
        {
            "id": str(uuid.uuid4()),
            "snap": str(snapshot_id),
            "wallet": str(wallet_id),
            "asset": str(asset_id),
        },
    )


async def _insert_complete_quote_set(session: AsyncSession, *, fetched_at_sql: str) -> None:
    await session.execute(
        text(
            f"""
            INSERT INTO quote_set (id, provider, fetched_at, status)
            VALUES (:id, 'coinmarketcap', {fetched_at_sql}, 'complete')
            """  # noqa: S608 — fetched_at_sql is a module-local constant, not user input
        ),
        {"id": str(uuid.uuid4())},
    )


@pytest.mark.integration
async def test_recently_fetched_price_reports_ok(
    db_session: AsyncSession, test_secret_key: str
) -> None:
    wallet_id, asset_id = await _insert_wallet_and_asset(db_session)
    await _insert_priced_snapshot(db_session, wallet_id=wallet_id, asset_id=asset_id)
    await _insert_complete_quote_set(db_session, fetched_at_sql="now()")
    await db_session.flush()

    with _no_coingecko_key:
        status = await get_quote_status(db_session)

    assert status.status == ComponentStatus.OK


@pytest.mark.integration
async def test_price_older_than_2x_freshness_reports_degraded(
    db_session: AsyncSession, test_secret_key: str
) -> None:
    """Default quote_refresh freshness_s is 3600s, so 5 hours is well past 2x."""
    wallet_id, asset_id = await _insert_wallet_and_asset(db_session)
    await _insert_priced_snapshot(db_session, wallet_id=wallet_id, asset_id=asset_id)
    await _insert_complete_quote_set(db_session, fetched_at_sql="now() - interval '5 hours'")
    await db_session.flush()

    with _no_coingecko_key:
        status = await get_quote_status(db_session)

    assert status.status == ComponentStatus.DEGRADED


@pytest.mark.integration
async def test_no_complete_quote_set_with_priced_lines_reports_degraded(
    db_session: AsyncSession, test_secret_key: str
) -> None:
    """Priced lines but zero complete quote_set rows is also a staleness signal."""
    wallet_id, asset_id = await _insert_wallet_and_asset(db_session)
    await _insert_priced_snapshot(db_session, wallet_id=wallet_id, asset_id=asset_id)
    await db_session.flush()

    with _no_coingecko_key:
        status = await get_quote_status(db_session)

    assert status.status == ComponentStatus.DEGRADED
