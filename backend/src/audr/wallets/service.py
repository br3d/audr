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


# A valuation line belongs to the wallet either directly or through the balance
# observation it was priced from; both reach the wallet, and the observation arm
# keeps a line whose wallet_id was somehow not set from blocking the delete.
_OWNED_VALUATION_LINE = (
    "(wallet_id = :wid OR observation_id IN ("
    "   SELECT id FROM balance_observation WHERE wallet_id = :wid"
    " ))"
)


async def delete_wallet(
    session: AsyncSession,
    *,
    wallet_id: uuid.UUID,
) -> dict[str, int]:
    """Permanently delete a wallet and every record derived from it (AUD-367).

    Unlike :func:`stop_wallet`, which only pauses scanning, this removes the
    address from the application entirely: balance observations, discovery
    coverage, monitored pairs, indexed on-chain events, the event-indexer
    checkpoint, and the wallet's valuation lines all go.  Valuation snapshots
    (and their history points) left without any line are removed too, so the
    history series does not keep reporting a total that included this wallet.

    Shared records that are not owned by the wallet — ``asset``, catalogue
    entries, quote sets — are left untouched.

    Returns a mapping of table name to deleted row count, for the audit trail.
    Raises WalletNotFoundError if the wallet does not exist.
    """
    await _get_or_raise(session, wallet_id)
    params: dict[str, object] = {"wid": str(wallet_id)}
    deleted: dict[str, int] = {}

    async def _delete(table: str, sql: str) -> None:
        stmt = sa.text(sql)
        if ":snapshot_ids" in sql:
            # Expanding bindparam renders a literal IN (...) list, which keeps
            # asyncpg from having to infer an array parameter type.
            stmt = stmt.bindparams(sa.bindparam("snapshot_ids", expanding=True))
        result = await session.execute(stmt, params)
        deleted[table] = int(result.rowcount or 0)

    # Snapshots this wallet contributed to, captured before its lines go: after
    # the delete there is no way left to tell them apart from snapshots that
    # never included this address.
    touched_snapshots = list(
        (
            # _OWNED_VALUATION_LINE is a fixed fragment; its value is bound, not interpolated.
            await session.execute(
                sa.text(
                    "SELECT DISTINCT snapshot_id FROM valuation_line"  # noqa: S608 — _OWNED_VALUATION_LINE is a fixed ":param" fragment, not interpolated
                    f" WHERE {_OWNED_VALUATION_LINE}"
                ),
                params,
            )
        ).scalars()
    )
    params["snapshot_ids"] = touched_snapshots

    # valuation_line references balance_observation as well as wallet, so it has
    # to go before the observations it was priced from — deleting observations
    # first trips fk_valuation_line_observation_id_balance_observation (AUD-394).
    await _delete(
        "valuation_line",
        f"DELETE FROM valuation_line WHERE {_OWNED_VALUATION_LINE}",  # noqa: S608 -- fixed ":param" fragment
    )
    # Child of balance_observation — must go before its parent.
    await _delete(
        "balance_observation_invalidation",
        "DELETE FROM balance_observation_invalidation"
        " WHERE observation_id IN ("
        "   SELECT id FROM balance_observation WHERE wallet_id = :wid"
        " )",
    )
    await _delete(
        "balance_observation",
        "DELETE FROM balance_observation WHERE wallet_id = :wid",
    )
    await _delete(
        "discovery_coverage",
        "DELETE FROM discovery_coverage WHERE wallet_id = :wid",
    )
    await _delete(
        "monitored_pair",
        "DELETE FROM monitored_pair WHERE wallet_id = :wid",
    )
    await _delete(
        "onchain_event",
        "DELETE FROM onchain_event WHERE wallet_id = :wid",
    )
    await _delete(
        "event_indexer_checkpoint",
        "DELETE FROM event_indexer_checkpoint WHERE wallet_id = :wid",
    )
    # history_point hangs off valuation_snapshot, so clear it for the snapshots
    # this wallet just emptied, then drop those snapshots.  Snapshots that
    # still carry lines from other wallets are kept as they are.
    if touched_snapshots:
        await _delete(
            "history_point",
            "DELETE FROM history_point"
            " WHERE snapshot_id IN :snapshot_ids"
            "   AND NOT EXISTS ("
            "     SELECT 1 FROM valuation_line vl"
            "     WHERE vl.snapshot_id = history_point.snapshot_id"
            "   )",
        )
        await _delete(
            "valuation_snapshot",
            "DELETE FROM valuation_snapshot"
            " WHERE id IN :snapshot_ids"
            "   AND NOT EXISTS ("
            "     SELECT 1 FROM valuation_line vl"
            "     WHERE vl.snapshot_id = valuation_snapshot.id"
            "   )",
        )
    else:
        deleted["history_point"] = 0
        deleted["valuation_snapshot"] = 0
    await _delete("wallet", "DELETE FROM wallet WHERE id = :wid")

    await session.flush()
    await session.commit()
    return deleted


async def list_wallets(session: AsyncSession) -> list[Wallet]:
    """Return all tracked wallets, ordered by creation time."""
    result = await session.execute(sa.select(Wallet).order_by(Wallet.created_at))
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
