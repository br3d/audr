"""Balance recording and holdings aggregation (T040–T041, not yet implemented).

Stubs are present so tests can be collected.  Full implementation lands in T040–T041.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession


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
    raise NotImplementedError("T040: balance scan worker not yet implemented")


async def get_holdings(
    session: AsyncSession,
    *,
    wallet_address: str,
) -> list[BalanceObservation]:
    raise NotImplementedError("T041: holdings aggregation not yet implemented")
