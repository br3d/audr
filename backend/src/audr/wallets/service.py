"""Wallet management service — add, label, stop, reactivate (T032 / US1)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from audr.wallets.models import Wallet


class WalletAlreadyExistsError(Exception):
    """Raised when adding a wallet with an address that is already tracked."""


class WalletNotFoundError(Exception):
    """Raised when a wallet_id does not exist."""


class InvalidAddressError(Exception):
    """Raised when the provided Ethereum address is malformed."""


def _normalize_address(address: str) -> str:
    """Lowercase and basic-validate an Ethereum address."""
    stripped = address.strip()
    if not stripped.startswith("0x") or len(stripped) != 42:
        raise InvalidAddressError(f"Invalid Ethereum address: {address!r}")
    try:
        int(stripped, 16)
    except ValueError as exc:
        raise InvalidAddressError(f"Invalid Ethereum address: {address!r}") from exc
    return stripped.lower()


async def add_wallet(
    session: AsyncSession,
    *,
    address: str,
    label: str = "",
) -> Wallet:
    """Add a new tracked wallet.  Raises WalletAlreadyExistsError on duplicate."""
    normalised = _normalize_address(address)
    wallet = Wallet(
        id=uuid.uuid4(),
        address=normalised,
        label=label,
        status="active",
    )
    session.add(wallet)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise WalletAlreadyExistsError(normalised) from exc
    await session.commit()
    return wallet


async def set_label(
    session: AsyncSession,
    *,
    wallet_id: uuid.UUID,
    label: str,
) -> Wallet:
    """Update the label of a wallet."""
    wallet = await _get_or_raise(session, wallet_id)
    wallet.label = label
    wallet.updated_at = datetime.now(tz=UTC)
    await session.flush()
    await session.commit()
    return wallet


async def stop_wallet(
    session: AsyncSession,
    *,
    wallet_id: uuid.UUID,
) -> Wallet:
    """Stop balance scanning for a wallet."""
    wallet = await _get_or_raise(session, wallet_id)
    wallet.status = "stopped"
    wallet.updated_at = datetime.now(tz=UTC)
    await session.flush()
    await session.commit()
    return wallet


async def reactivate_wallet(
    session: AsyncSession,
    *,
    wallet_id: uuid.UUID,
) -> Wallet:
    """Resume balance scanning for a wallet."""
    wallet = await _get_or_raise(session, wallet_id)
    wallet.status = "active"
    wallet.updated_at = datetime.now(tz=UTC)
    await session.flush()
    await session.commit()
    return wallet


async def list_wallets(session: AsyncSession) -> list[Wallet]:
    """Return all tracked wallets, ordered by creation time."""
    result = await session.execute(
        sa.select(Wallet).order_by(Wallet.created_at)
    )
    return list(result.scalars())


async def list_wallets_page(
    session: AsyncSession,
    *,
    cursor: uuid.UUID | None = None,
    limit: int = 20,
) -> tuple[list[Wallet], uuid.UUID | None]:
    """Return up to *limit* wallets, oldest first, with keyset pagination.

    Returns (wallets, next_cursor). Pass next_cursor back in as *cursor* to
    fetch the following page; None means the list is exhausted.
    """
    stmt = sa.select(Wallet).order_by(Wallet.created_at, Wallet.id).limit(limit + 1)
    if cursor is not None:
        cursor_wallet = await session.get(Wallet, cursor)
        if cursor_wallet is not None:
            stmt = stmt.where(
                sa.tuple_(Wallet.created_at, Wallet.id)
                > sa.tuple_(cursor_wallet.created_at, cursor_wallet.id)
            )

    result = await session.execute(stmt)
    wallets = list(result.scalars())
    has_more = len(wallets) > limit
    wallets = wallets[:limit]
    next_cursor = wallets[-1].id if has_more and wallets else None
    return wallets, next_cursor


async def get_wallet(
    session: AsyncSession,
    *,
    wallet_id: uuid.UUID,
) -> Wallet | None:
    """Fetch a wallet by ID or return None."""
    return await session.get(Wallet, wallet_id)


async def _get_or_raise(session: AsyncSession, wallet_id: uuid.UUID) -> Wallet:
    wallet = await session.get(Wallet, wallet_id)
    if wallet is None:
        raise WalletNotFoundError(str(wallet_id))
    return wallet
