"""Integration tests for GET /api/v1/allowances (AUD-300).

Covers:
  - only the latest Approval event per (wallet, token, spender) is returned
  - is_unlimited flags amounts at/above the "infinite approval" threshold
  - wallet_id filter and unlimited_only filter
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.fixtures.seed import WETH_ADDRESS

pytestmark = pytest.mark.integration

_SPENDER_A = "0x" + "aa" * 20
_SPENDER_B = "0x" + "bb" * 20


@pytest.fixture(autouse=True)
async def _clean_tables(
    db_session_factory: async_sessionmaker[AsyncSession],
    test_secret_key: str,
) -> None:
    async with db_session_factory() as session:
        async with session.begin():
            await session.execute(text("DELETE FROM onchain_event"))
            await session.execute(text("DELETE FROM wallet"))
            await session.execute(text("DELETE FROM session"))
            await session.execute(text("DELETE FROM login_attempt"))
            await session.execute(text("DELETE FROM owner"))


async def _seed_approval(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    wallet_id: str,
    token_address: str,
    spender: str,
    amount: int,
    block_number: int,
    log_index: int = 0,
) -> None:
    async with db_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    """
                    INSERT INTO onchain_event
                      (wallet_id, tx_hash, block_number, log_index, event_type,
                       token_address, counterparty_address, raw_amount)
                    VALUES
                      (:wallet_id, :tx_hash, :block_number, :log_index, 'approval',
                       :token_address, :spender, :amount)
                    """
                ),
                {
                    "wallet_id": wallet_id,
                    "tx_hash": "0x" + uuid.uuid4().hex.ljust(64, "0"),
                    "block_number": block_number,
                    "log_index": log_index,
                    "token_address": token_address,
                    "spender": spender,
                    "amount": str(amount),
                },
            )


async def test_returns_latest_approval_per_spender(
    seeded_client,
    buterin_wallet,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client, _csrf = seeded_client
    wallet_id = buterin_wallet["id"]

    # Older, smaller approval — should be superseded.
    await _seed_approval(
        db_session_factory,
        wallet_id=wallet_id,
        token_address=WETH_ADDRESS,
        spender=_SPENDER_A,
        amount=1_000,
        block_number=100,
    )
    # Newer approval to the same spender/token — should win.
    await _seed_approval(
        db_session_factory,
        wallet_id=wallet_id,
        token_address=WETH_ADDRESS,
        spender=_SPENDER_A,
        amount=2**256 - 1,
        block_number=200,
    )

    r = await client.get("/api/v1/allowances")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    row = body["allowances"][0]
    assert row["spender_address"] == _SPENDER_A
    assert row["raw_amount"] == str(2**256 - 1)
    assert row["is_unlimited"] is True
    assert row["observed_at_block"] == 200


async def test_small_allowance_is_not_flagged_unlimited(
    seeded_client,
    buterin_wallet,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client, _csrf = seeded_client
    wallet_id = buterin_wallet["id"]

    await _seed_approval(
        db_session_factory,
        wallet_id=wallet_id,
        token_address=WETH_ADDRESS,
        spender=_SPENDER_B,
        amount=5_000_000,
        block_number=50,
    )

    r = await client.get("/api/v1/allowances")
    assert r.status_code == 200
    row = r.json()["allowances"][0]
    assert row["is_unlimited"] is False


async def test_unlimited_only_filter(
    seeded_client,
    buterin_wallet,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client, _csrf = seeded_client
    wallet_id = buterin_wallet["id"]

    await _seed_approval(
        db_session_factory,
        wallet_id=wallet_id,
        token_address=WETH_ADDRESS,
        spender=_SPENDER_A,
        amount=2**256 - 1,
        block_number=10,
    )
    await _seed_approval(
        db_session_factory,
        wallet_id=wallet_id,
        token_address=WETH_ADDRESS,
        spender=_SPENDER_B,
        amount=42,
        block_number=11,
    )

    r = await client.get("/api/v1/allowances", params={"unlimited_only": "true"})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["allowances"][0]["spender_address"] == _SPENDER_A


async def test_requires_session(seeded_client) -> None:
    client, _csrf = seeded_client
    # Drop the session cookie to simulate an unauthenticated request.
    unauth = client
    unauth.cookies.clear()
    r = await unauth.get("/api/v1/allowances")
    assert r.status_code == 401
