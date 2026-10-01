"""Balance recording and holdings aggregation (T040 / T041 / US1).

record_balance() stores a single observation with exact integer raw units.
get_holdings() returns the latest observation per (wallet, asset) pair.

Rules:
- Exact integer arithmetic — no floats, no rounding.
- Unscanned tokens simply do not appear in the holdings list (unknown ≠ zero).
- Confirmed zero balances (block scanned, result = 0) are stored as 0 and returned.
- Address case: callers may pass mixed-case; we normalise to lowercase.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from audr.assets.constants import (
    NATIVE_ETH_ADDRESS,
    NATIVE_ETH_DECIMALS,
    NATIVE_ETH_NAME,
    NATIVE_ETH_SOURCE,
    NATIVE_ETH_SYMBOL,
    is_native_eth,
    normalise_token_address,
)
from audr.assets.models import Asset
from audr.wallets.models import Wallet


@dataclass
class BalanceObservation:
    wallet_address: str
    token_address: str
    raw_amount: int | None
    block_number: int | None


async def record_balance(
    session: AsyncSession,
    *,
    wallet_address: str,
    token_address: str,
    raw_amount: int,
    block_number: int,
) -> None:
    """Record a balance observation.

    Upserts the wallet and asset rows if they don't already exist (using
    'manual' source for auto-created assets).  Then inserts a new
    balance_observation row — the history is append-only.
    """
    if not isinstance(raw_amount, int):
        raise TypeError(f"raw_amount must be int, got {type(raw_amount)}")

    addr = wallet_address.lower()
    taddr = token_address.lower()

    wallet_id = await _ensure_wallet(session, addr)
    asset_id = await _ensure_asset(session, taddr)

    await session.execute(
        sa.text(
            """
            INSERT INTO balance_observation
              (id, wallet_id, asset_id, raw_amount, block_number, observed_at)
            VALUES
              (:id, :wallet_id, :asset_id, :raw_amount, :block_number, :now)
            """
        ),
        {
            "id": str(uuid.uuid4()),
            "wallet_id": str(wallet_id),
            "asset_id": str(asset_id),
            "raw_amount": raw_amount,
            "block_number": block_number,
            "now": datetime.now(tz=UTC),
        },
    )
    await session.flush()


async def get_holdings(
    session: AsyncSession,
    *,
    wallet_address: str,
) -> list[BalanceObservation]:
    """Return the latest balance observation per asset for *wallet_address*.

    Only assets for which at least one observation exists are returned.
    """
    addr = wallet_address.lower()

    result = await session.execute(
        sa.text(
            """
            SELECT
                w.address   AS wallet_address,
                a.token_address,
                bo.raw_amount::numeric AS raw_amount,
                bo.block_number
            FROM balance_observation bo
            JOIN wallet w ON w.id = bo.wallet_id
            JOIN asset  a ON a.id = bo.asset_id
            WHERE w.address = :addr
              AND bo.observed_at = (
                  SELECT MAX(bo2.observed_at)
                  FROM balance_observation bo2
                  WHERE bo2.wallet_id = bo.wallet_id
                    AND bo2.asset_id  = bo.asset_id
              )
            """
        ),
        {"addr": addr},
    )
    return [
        BalanceObservation(
            wallet_address=row[0],
            token_address=row[1],
            raw_amount=int(row[2]),
            block_number=int(row[3]),
        )
        for row in result
    ]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _ensure_wallet(session: AsyncSession, address: str) -> uuid.UUID:
    """Return the wallet ID for *address*, inserting a row if absent."""
    result = await session.execute(
        sa.select(Wallet.id).where(Wallet.address == address)
    )
    row = result.first()
    if row is not None:
        return uuid.UUID(str(row[0]))
    wallet = Wallet(id=uuid.uuid4(), address=address, label="", status="active")
    session.add(wallet)
    await session.flush()
    return wallet.id


async def _ensure_asset(session: AsyncSession, token_address: str) -> uuid.UUID:
    """Return the asset ID for *token_address*, inserting a row if absent.

    Native ETH is created with its real identity rather than an ``UNKNOWN``
    placeholder (AUD-360) — minting a placeholder for the native sentinel is a
    silent failure that surfaces to the user as an unnamed top holding.
    """
    native = is_native_eth(token_address)
    canonical = normalise_token_address(token_address) if native else token_address

    result = await session.execute(
        sa.select(Asset.id).where(Asset.token_address == canonical)
    )
    row = result.first()
    if row is not None:
        return uuid.UUID(str(row[0]))

    if native:
        asset = Asset(
            id=uuid.uuid4(),
            token_address=NATIVE_ETH_ADDRESS,
            symbol=NATIVE_ETH_SYMBOL,
            name=NATIVE_ETH_NAME,
            decimals=NATIVE_ETH_DECIMALS,
            source=NATIVE_ETH_SOURCE,
        )
    else:
        asset = Asset(
            id=uuid.uuid4(),
            token_address=canonical,
            symbol="UNKNOWN",
            name="Unknown Token",
            decimals=18,
            source="manual",
        )
    session.add(asset)
    await session.flush()
    return asset.id
