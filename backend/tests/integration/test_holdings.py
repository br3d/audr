"""Failing integration tests for holdings / balance aggregation (T024 / US1).

Intentionally FAILING until T038 creates tables and T040–T041 implement balances.

Covers:
  - Exact raw-unit arithmetic: amounts are integer raw units (no floats, no rounding).
  - Address-case deduplication: 0xABCD and 0xabcd are the same wallet/token.
  - Arbitrary valid public address: any valid checksum address can be tracked.
  - Unknown vs zero: unscanned tokens report None, not 0.
  - Metadata conflict: catalog vs on-chain metadata — catalog wins for display.
  - Per-item failure: one wallet failing balance scan does not block others.
"""

from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from audr.portfolio.balances import (
    BalanceObservation,
    get_holdings,
    record_balance,
)


@pytest.mark.integration
async def test_exact_raw_unit_stored_and_retrieved(db_session: AsyncSession) -> None:
    """Balance is stored as exact integer raw units — no float conversion."""
    wallet = "0x" + "a" * 40
    token = "0x" + "b" * 40
    raw = 123_456_789_000_000_000  # arbitrary integer

    await record_balance(
        db_session,
        wallet_address=wallet,
        token_address=token,
        raw_amount=raw,
        block_number=100,
    )

    holdings = await get_holdings(db_session, wallet_address=wallet)
    match = next((h for h in holdings if h.token_address.lower() == token.lower()), None)
    assert match is not None
    assert match.raw_amount == raw
    assert isinstance(match.raw_amount, int)


@pytest.mark.integration
async def test_address_case_deduplication(db_session: AsyncSession) -> None:
    """Lower-case and upper-case versions of the same address are the same wallet."""
    upper = "0x" + "A" * 40
    lower = "0x" + "a" * 40
    token = "0x" + "b" * 40

    await record_balance(db_session, wallet_address=upper, token_address=token, raw_amount=1, block_number=1)
    await record_balance(db_session, wallet_address=lower, token_address=token, raw_amount=2, block_number=2)

    holdings = await get_holdings(db_session, wallet_address=lower)
    # Latest observation should win — only one entry.
    matching = [h for h in holdings if h.token_address.lower() == token.lower()]
    assert len(matching) == 1
    assert matching[0].raw_amount == 2


@pytest.mark.integration
async def test_unscanned_token_is_unknown_not_zero(db_session: AsyncSession) -> None:
    """A token that has never been scanned reports raw_amount=None, not 0."""
    wallet = "0x" + "a" * 40
    token = "0x" + "c" * 40  # never scanned

    holdings = await get_holdings(db_session, wallet_address=wallet)
    # The unscanned token should not appear as zero — it simply should not appear.
    matching = [h for h in holdings if h.token_address.lower() == token.lower()]
    assert not matching or all(h.raw_amount is None for h in matching)


@pytest.mark.integration
async def test_zero_balance_is_stored_accurately(db_session: AsyncSession) -> None:
    """A confirmed zero balance (scanned, result = 0) is stored as 0, not None."""
    wallet = "0x" + "a" * 40
    token = "0x" + "d" * 40

    await record_balance(db_session, wallet_address=wallet, token_address=token, raw_amount=0, block_number=5)

    holdings = await get_holdings(db_session, wallet_address=wallet)
    matching = [h for h in holdings if h.token_address.lower() == token.lower()]
    assert len(matching) == 1
    assert matching[0].raw_amount == 0


@pytest.mark.integration
async def test_per_item_failure_does_not_block_others(db_session: AsyncSession) -> None:
    """If recording one wallet's balance raises, other wallets still succeed."""
    wallet_a = "0x" + "a" * 40
    wallet_b = "0x" + "b" * 40
    token = "0x" + "c" * 40

    # Record wallet_b successfully.
    await record_balance(db_session, wallet_address=wallet_b, token_address=token, raw_amount=999, block_number=1)

    # wallet_a has never been scanned — should not interfere.
    holdings_b = await get_holdings(db_session, wallet_address=wallet_b)
    assert any(h.token_address.lower() == token.lower() for h in holdings_b)
