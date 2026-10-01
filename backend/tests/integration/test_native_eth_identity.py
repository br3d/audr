"""Regression tests for native-ETH asset identity (AUD-360).

The original defect: the jobs and price providers wrote native ETH balances
under ``0xeeee…eeee`` while ``/portfolio`` and ``/assets`` tested for
``0x0000…0000``. Nothing ever matched, so ``is_native`` was always ``False``
and ``_ensure_asset`` minted an ``UNKNOWN / Unknown Token`` placeholder for the
native sentinel — the largest holding in the portfolio rendered as ``UNKNOWN``.

These tests pin the single sentinel and the real ETH identity.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from audr.assets.constants import (
    LEGACY_NATIVE_ETH_ADDRESS,
    NATIVE_ETH_ADDRESS,
    is_native_eth,
    normalise_token_address,
)
from audr.portfolio.balances import record_balance

pytestmark = pytest.mark.integration


def test_writers_and_readers_agree_on_one_sentinel() -> None:
    """Every module must resolve native ETH to the same address."""
    from audr.jobs import quotes as quotes_job
    from audr.providers import coingecko_demo, coinmarketcap_public

    assert quotes_job.NATIVE_ETH_ADDRESS == NATIVE_ETH_ADDRESS
    assert coingecko_demo.NATIVE_ETH_ADDRESS == NATIVE_ETH_ADDRESS
    assert coinmarketcap_public.NATIVE_ETH_ADDRESS == NATIVE_ETH_ADDRESS


def test_is_native_eth_accepts_both_sentinels_and_rejects_erc20() -> None:
    assert is_native_eth(NATIVE_ETH_ADDRESS)
    assert is_native_eth(LEGACY_NATIVE_ETH_ADDRESS)
    assert is_native_eth(NATIVE_ETH_ADDRESS.upper())
    assert not is_native_eth("0x" + "a" * 40)
    assert not is_native_eth(None)
    assert not is_native_eth("")


def test_normalise_collapses_legacy_sentinel() -> None:
    assert normalise_token_address(LEGACY_NATIVE_ETH_ADDRESS) == NATIVE_ETH_ADDRESS
    assert normalise_token_address(NATIVE_ETH_ADDRESS.upper()) == NATIVE_ETH_ADDRESS
    erc20 = "0x" + "B" * 40
    assert normalise_token_address(erc20) == erc20.lower()


async def test_recording_native_balance_creates_eth_not_unknown(
    db_session: AsyncSession,
) -> None:
    """The native sentinel must never mint an UNKNOWN placeholder asset."""
    await record_balance(
        db_session,
        wallet_address="0x" + "3" * 40,
        token_address=NATIVE_ETH_ADDRESS,
        raw_amount=16_682_917_450_871_140_994,
        block_number=21_000_000,
    )

    row = (
        await db_session.execute(
            sa.text(
                "SELECT symbol, name, decimals, source FROM asset"
                " WHERE token_address = :a"
            ),
            {"a": NATIVE_ETH_ADDRESS},
        )
    ).first()

    assert row is not None, "native ETH asset row was not created"
    symbol, name, decimals, source = row
    assert symbol == "ETH"
    assert name == "Ethereum"
    assert int(decimals) == 18
    assert source != "manual"


async def test_legacy_native_address_resolves_to_canonical_asset(
    db_session: AsyncSession,
) -> None:
    """A legacy-sentinel write must land on the canonical ETH row, not a second one."""
    wallet = "0x" + "4" * 40

    await record_balance(
        db_session,
        wallet_address=wallet,
        token_address=NATIVE_ETH_ADDRESS,
        raw_amount=10**18,
        block_number=21_000_001,
    )
    await record_balance(
        db_session,
        wallet_address=wallet,
        token_address=LEGACY_NATIVE_ETH_ADDRESS,
        raw_amount=2 * 10**18,
        block_number=21_000_002,
    )

    count = (
        await db_session.execute(
            sa.text(
                "SELECT count(*) FROM asset WHERE token_address IN (:canonical, :legacy)"
            ),
            {"canonical": NATIVE_ETH_ADDRESS, "legacy": LEGACY_NATIVE_ETH_ADDRESS},
        )
    ).scalar()

    assert count == 1, "legacy sentinel created a duplicate native asset row"
