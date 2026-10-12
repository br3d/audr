"""Integration tests for the human-readable enrichment fields on
GET /api/v1/events (AUD-522): asset_id, symbol, token_name, decimals,
amount, value_usd, price_usd, wallet_label.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.fixtures.seed import WETH_ADDRESS

pytestmark = pytest.mark.integration

_UNKNOWN_TOKEN = "0x" + "cc" * 20


async def _wipe(db_session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with db_session_factory() as session:
        async with session.begin():
            await session.execute(sa.text("DELETE FROM onchain_event"))
            await session.execute(sa.text("DELETE FROM quote_observation"))
            await session.execute(sa.text("DELETE FROM quote_set"))
            await session.execute(sa.text("DELETE FROM asset"))
            await session.execute(sa.text("DELETE FROM wallet"))
            await session.execute(sa.text("DELETE FROM session"))
            await session.execute(sa.text("DELETE FROM login_attempt"))
            await session.execute(sa.text("DELETE FROM owner"))


@pytest.fixture(autouse=True)
async def _clean_tables(
    db_session_factory: async_sessionmaker[AsyncSession],
    test_secret_key: str,
) -> AsyncGenerator[None]:
    # Wipe after too: other test modules' own cleanup fixtures delete `asset`
    # without first deleting `quote_observation`/`quote_set` (they don't know
    # about this module's rows), so leftovers here would FK-violate whichever
    # test runs next.
    await _wipe(db_session_factory)
    yield
    await _wipe(db_session_factory)


async def _seed_weth_asset(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> str:
    """Insert the WETH asset directly (source='manual', matching the
    convention in test_valuation.py) rather than via the `seeded_assets`
    fixture, which inserts source='seed' — a value the `ck_asset_source`
    check constraint has never allowed (catalog|manual only) and that no
    other test exercises.
    """
    asset_id = uuid.uuid4()
    async with db_session_factory() as session:
        async with session.begin():
            await session.execute(
                sa.text(
                    """
                    INSERT INTO asset (id, token_address, symbol, name, decimals, source, excluded)
                    VALUES (:id, :addr, 'WETH', 'Wrapped Ether', 18, 'manual', false)
                    """
                ),
                {"id": str(asset_id), "addr": WETH_ADDRESS},
            )
    return str(asset_id)


async def _seed_transfer(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    wallet_id: str,
    token_address: str,
    counterparty: str | None,
    amount: int,
    block_number: int,
    log_index: int = 0,
    event_type: str = "transfer_in",
) -> None:
    async with db_session_factory() as session:
        async with session.begin():
            await session.execute(
                sa.text(
                    """
                    INSERT INTO onchain_event
                      (wallet_id, tx_hash, block_number, log_index, event_type,
                       token_address, counterparty_address, raw_amount)
                    VALUES
                      (:wallet_id, :tx_hash, :block_number, :log_index, :event_type,
                       :token_address, :counterparty, :amount)
                    """
                ),
                {
                    "wallet_id": wallet_id,
                    "tx_hash": "0x" + uuid.uuid4().hex.ljust(64, "0"),
                    "block_number": block_number,
                    "log_index": log_index,
                    "event_type": event_type,
                    "token_address": token_address,
                    "counterparty": counterparty,
                    "amount": str(amount),
                },
            )


async def _seed_quote(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    asset_id: str,
    price_usd: Decimal,
    status: str = "complete",
) -> None:
    async with db_session_factory() as session:
        async with session.begin():
            qset_id = uuid.uuid4()
            await session.execute(
                sa.text(
                    "INSERT INTO quote_set (id, provider, fetched_at, status)"
                    " VALUES (:id, 'coingecko', now(), :status)"
                ),
                {"id": str(qset_id), "status": status},
            )
            await session.execute(
                sa.text(
                    "INSERT INTO quote_observation (id, quote_set_id, asset_id, price_usd)"
                    " VALUES (:id, :qset, :asset, :price)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "qset": str(qset_id),
                    "asset": asset_id,
                    "price": str(price_usd),
                },
            )


async def test_known_asset_event_returns_amount_and_value(
    seeded_client,
    buterin_wallet,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client, _csrf = seeded_client
    wallet_id = buterin_wallet["id"]
    weth_id = await _seed_weth_asset(db_session_factory)

    await _seed_transfer(
        db_session_factory,
        wallet_id=wallet_id,
        token_address=WETH_ADDRESS,
        counterparty="0x" + "11" * 20,
        amount=2 * 10**18,
        block_number=100,
    )
    await _seed_quote(db_session_factory, asset_id=weth_id, price_usd=Decimal("1000"))

    r = await client.get("/api/v1/events")
    assert r.status_code == 200
    row = r.json()["events"][0]

    assert row["asset_id"] == weth_id
    assert row["symbol"] == "WETH"
    assert row["token_name"] == "Wrapped Ether"
    assert row["decimals"] == 18
    assert row["amount"] == "2.000000000000000000"
    assert row["price_usd"] == "1000.000000000000000000"
    assert row["value_usd"] == "2000.000000000000000000"
    assert row["wallet_label"] == buterin_wallet["label"]
    # raw_amount stays the untouched source of truth
    assert row["raw_amount"] == str(2 * 10**18)


async def test_unknown_token_event_returns_nulls_and_200(
    seeded_client,
    buterin_wallet,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client, _csrf = seeded_client
    wallet_id = buterin_wallet["id"]

    await _seed_transfer(
        db_session_factory,
        wallet_id=wallet_id,
        token_address=_UNKNOWN_TOKEN,
        counterparty="0x" + "22" * 20,
        amount=12345,
        block_number=200,
    )

    r = await client.get("/api/v1/events")
    assert r.status_code == 200
    row = r.json()["events"][0]

    assert row["asset_id"] is None
    assert row["symbol"] is None
    assert row["token_name"] is None
    assert row["decimals"] is None
    assert row["amount"] is None
    assert row["value_usd"] is None
    assert row["price_usd"] is None
    assert row["raw_amount"] == "12345"


async def test_known_asset_without_price_returns_amount_but_null_value(
    seeded_client,
    buterin_wallet,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """unknown != 0: a known asset with no priced quote_set gets a null
    value_usd/price_usd, not zero, even though amount is computable."""
    client, _csrf = seeded_client
    wallet_id = buterin_wallet["id"]
    await _seed_weth_asset(db_session_factory)

    await _seed_transfer(
        db_session_factory,
        wallet_id=wallet_id,
        token_address=WETH_ADDRESS,
        counterparty="0x" + "33" * 20,
        amount=10**18,
        block_number=300,
    )

    r = await client.get("/api/v1/events")
    assert r.status_code == 200
    row = r.json()["events"][0]

    assert row["decimals"] == 18
    assert row["amount"] == "1.000000000000000000"
    assert row["price_usd"] is None
    assert row["value_usd"] is None


async def test_requires_session(seeded_client) -> None:
    client, _csrf = seeded_client
    unauth = client
    unauth.cookies.clear()
    r = await unauth.get("/api/v1/events")
    assert r.status_code == 401


async def test_events_page_issues_constant_query_count_regardless_of_row_count(
    seeded_client,
    buterin_wallet,
    db_session_factory: async_sessionmaker[AsyncSession],
    db_engine,
) -> None:
    """No N+1: asset/quote enrichment and wallet-label decryption are each a
    single query for the whole page, not one per event row."""
    client, _csrf = seeded_client
    wallet_id = buterin_wallet["id"]
    weth_id = await _seed_weth_asset(db_session_factory)
    await _seed_quote(db_session_factory, asset_id=weth_id, price_usd=Decimal("1000"))

    for i in range(5):
        await _seed_transfer(
            db_session_factory,
            wallet_id=wallet_id,
            token_address=WETH_ADDRESS,
            counterparty="0x" + f"{i:02x}" * 20,
            amount=10**18,
            block_number=1000 + i,
        )

    query_count = 0

    def _count(*_args: object, **_kwargs: object) -> None:
        nonlocal query_count
        query_count += 1

    sa.event.listen(db_engine.sync_engine, "before_cursor_execute", _count)
    try:
        r = await client.get("/api/v1/events")
    finally:
        sa.event.remove(db_engine.sync_engine, "before_cursor_execute", _count)

    assert r.status_code == 200
    assert len(r.json()["events"]) == 5
    five_row_count = query_count

    await _seed_transfer(
        db_session_factory,
        wallet_id=wallet_id,
        token_address=WETH_ADDRESS,
        counterparty="0x" + "99" * 20,
        amount=10**18,
        block_number=2000,
    )

    query_count = 0
    sa.event.listen(db_engine.sync_engine, "before_cursor_execute", _count)
    try:
        r = await client.get("/api/v1/events")
    finally:
        sa.event.remove(db_engine.sync_engine, "before_cursor_execute", _count)

    assert r.status_code == 200
    assert len(r.json()["events"]) == 6
    assert query_count == five_row_count, (
        f"query count grew with row count: {five_row_count} -> {query_count}"
    )
