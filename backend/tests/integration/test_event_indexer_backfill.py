"""Integration tests: the first time event_indexer sees a wallet it starts
`event_indexer_backfill_blocks` behind the chain tip and indexes that range,
rather than checkpointing at the tip and showing an empty Events/Allowances
view until the wallet next transacts (AUD-445)."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from audr.jobs.event_indexer import _index_wallet
from audr.providers.rpc_reader import APPROVAL_TOPIC, LOG_CHUNK_SIZE
from tests.helpers import wallet_address_columns

_WALLET_ADDRESS = "0x" + "11" * 20
_TOKEN_ADDRESS = "0x" + "cc" * 20


async def _insert_wallet(session: AsyncSession, address: str) -> uuid.UUID:
    wallet_id = uuid.uuid4()
    cols = await wallet_address_columns(session, wallet_id, address)
    await session.execute(
        sa.text(
            "INSERT INTO wallet (id, address_ciphertext, address_bidx, label_ciphertext)"
            " VALUES (:id, :addr_ct, :addr_bidx, '')"
        ),
        {
            "id": str(wallet_id),
            "addr_ct": cols["address_ciphertext"],
            "addr_bidx": cols["address_bidx"],
        },
    )
    return wallet_id


async def _checkpoint(session: AsyncSession, wallet_id: uuid.UUID) -> int:
    row = await session.execute(
        sa.text("SELECT last_processed_block FROM event_indexer_checkpoint WHERE wallet_id = :wid"),
        {"wid": str(wallet_id)},
    )
    return int(row.scalar_one())


def _fake_reader(log_chunk_size: int = LOG_CHUNK_SIZE) -> AsyncMock:
    reader = AsyncMock()
    reader.get_logs = AsyncMock(return_value=[])
    # Real readers expose this as a plain int property that narrows when an
    # endpoint rejects a range as too wide (AUD-445); an AsyncMock attribute
    # would hand the indexer a MagicMock to do arithmetic on.
    reader.log_chunk_size = log_chunk_size
    return reader


@pytest.mark.integration
async def test_first_sight_backfills_configured_window(db_session: AsyncSession) -> None:
    wallet_id = await _insert_wallet(db_session, _WALLET_ADDRESS)
    await db_session.flush()

    current_block = 100 * LOG_CHUNK_SIZE
    backfill = 3 * LOG_CHUNK_SIZE
    reader = _fake_reader()

    inserted, chunks_used = await _index_wallet(
        db_session,
        reader,
        wallet_id=wallet_id,
        wallet_address=_WALLET_ADDRESS,
        token_addresses=[_TOKEN_ADDRESS],
        current_block=current_block,
        max_chunks=10,
        backfill_blocks=backfill,
    )

    assert inserted == 0
    assert chunks_used == 3
    assert await _checkpoint(db_session, wallet_id) == current_block

    # Three log queries (out/in/approval) per chunk, and the first range must
    # start one block after the backfill floor rather than at the tip.
    assert reader.get_logs.await_count == 9
    first_call = reader.get_logs.await_args_list[0].kwargs
    assert first_call["from_block"] == current_block - backfill + 1


@pytest.mark.integration
async def test_zero_backfill_keeps_index_from_now_behaviour(db_session: AsyncSession) -> None:
    """backfill_blocks=0 is the documented opt-out: checkpoint at the tip and
    index nothing on first sight."""
    wallet_id = await _insert_wallet(db_session, _WALLET_ADDRESS)
    await db_session.flush()

    current_block = 100 * LOG_CHUNK_SIZE
    reader = _fake_reader()

    inserted, chunks_used = await _index_wallet(
        db_session,
        reader,
        wallet_id=wallet_id,
        wallet_address=_WALLET_ADDRESS,
        token_addresses=[_TOKEN_ADDRESS],
        current_block=current_block,
        max_chunks=10,
        backfill_blocks=0,
    )

    assert (inserted, chunks_used) == (0, 0)
    assert await _checkpoint(db_session, wallet_id) == current_block
    reader.get_logs.assert_not_awaited()


@pytest.mark.integration
async def test_backfill_floor_clamps_at_genesis(db_session: AsyncSession) -> None:
    """A backfill window wider than the chain's history must not produce a
    negative fromBlock."""
    wallet_id = await _insert_wallet(db_session, _WALLET_ADDRESS)
    await db_session.flush()

    reader = _fake_reader()
    await _index_wallet(
        db_session,
        reader,
        wallet_id=wallet_id,
        wallet_address=_WALLET_ADDRESS,
        token_addresses=[_TOKEN_ADDRESS],
        current_block=10,
        max_chunks=10,
        backfill_blocks=100 * LOG_CHUNK_SIZE,
    )

    assert reader.get_logs.await_args_list[0].kwargs["from_block"] == 1


@pytest.mark.integration
async def test_approval_query_is_not_restricted_to_tracked_tokens(
    db_session: AsyncSession,
) -> None:
    """Transfer queries filter to the tracked ERC-20 catalog (anyone can
    airdrop a Transfer log at our wallet), but an Approval with owner == our
    wallet is always one we signed, and the riskiest ones are on tokens the
    catalog never heard of (AUD-445)."""
    wallet_id = await _insert_wallet(db_session, _WALLET_ADDRESS)
    await db_session.flush()

    reader = _fake_reader()
    await _index_wallet(
        db_session,
        reader,
        wallet_id=wallet_id,
        wallet_address=_WALLET_ADDRESS,
        token_addresses=[_TOKEN_ADDRESS],
        current_block=LOG_CHUNK_SIZE,
        max_chunks=1,
        backfill_blocks=LOG_CHUNK_SIZE,
    )

    calls = [c.kwargs for c in reader.get_logs.await_args_list]
    approval_calls = [c for c in calls if c["topics"][0] == APPROVAL_TOPIC]
    transfer_calls = [c for c in calls if c["topics"][0] != APPROVAL_TOPIC]

    assert len(approval_calls) == 1
    assert approval_calls[0].get("address") is None
    assert transfer_calls
    assert all(c["address"] == [_TOKEN_ADDRESS] for c in transfer_calls)
