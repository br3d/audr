"""Integration tests: event_indexer caps how many eth_getLogs chunks it
processes in a single run and resumes the rest from the checkpoint on the
next run, instead of draining the whole catch-up range (and the RPC rate
budget) in one go (AUD-362)."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from audr.jobs.event_indexer import _index_wallet
from audr.providers.rpc_reader import LOG_CHUNK_SIZE
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


async def _insert_checkpoint(session: AsyncSession, wallet_id: uuid.UUID, block: int) -> None:
    await session.execute(
        sa.text(
            "INSERT INTO event_indexer_checkpoint (wallet_id, last_processed_block) "
            "VALUES (:wid, :block)"
        ),
        {"wid": str(wallet_id), "block": block},
    )


def _fake_reader(log_chunk_size: int = LOG_CHUNK_SIZE) -> AsyncMock:
    reader = AsyncMock()
    reader.get_logs = AsyncMock(return_value=[])
    # Real readers expose this as a plain int property that narrows when an
    # endpoint rejects a range as too wide (AUD-445); an AsyncMock attribute
    # would hand the indexer a MagicMock to do arithmetic on.
    reader.log_chunk_size = log_chunk_size
    return reader


@pytest.mark.integration
async def test_chunk_budget_caps_work_and_persists_partial_checkpoint(
    db_session: AsyncSession,
) -> None:
    wallet_id = await _insert_wallet(db_session, _WALLET_ADDRESS)
    await _insert_checkpoint(db_session, wallet_id, 0)
    await db_session.flush()

    current_block = 5 * LOG_CHUNK_SIZE
    reader = _fake_reader()

    inserted, chunks_used = await _index_wallet(
        db_session,
        reader,
        wallet_id=wallet_id,
        wallet_address=_WALLET_ADDRESS,
        token_addresses=[_TOKEN_ADDRESS],
        current_block=current_block,
        max_chunks=2,
    )

    assert inserted == 0
    assert chunks_used == 2
    # Three log queries (out/in/approval) per chunk, capped at 2 chunks.
    assert reader.get_logs.await_count == 6

    checkpoint_row = await db_session.execute(
        sa.text("SELECT last_processed_block FROM event_indexer_checkpoint WHERE wallet_id = :wid"),
        {"wid": str(wallet_id)},
    )
    assert checkpoint_row.scalar_one() == 2 * LOG_CHUNK_SIZE


@pytest.mark.integration
async def test_chunk_budget_resumes_remaining_range_on_next_call(
    db_session: AsyncSession,
) -> None:
    """A second call with a fresh budget picks up exactly where the first left off."""
    wallet_id = await _insert_wallet(db_session, _WALLET_ADDRESS)
    await _insert_checkpoint(db_session, wallet_id, 0)
    await db_session.flush()

    current_block = 5 * LOG_CHUNK_SIZE
    reader = _fake_reader()

    await _index_wallet(
        db_session,
        reader,
        wallet_id=wallet_id,
        wallet_address=_WALLET_ADDRESS,
        token_addresses=[_TOKEN_ADDRESS],
        current_block=current_block,
        max_chunks=2,
    )

    inserted, chunks_used = await _index_wallet(
        db_session,
        reader,
        wallet_id=wallet_id,
        wallet_address=_WALLET_ADDRESS,
        token_addresses=[_TOKEN_ADDRESS],
        current_block=current_block,
        max_chunks=10,
    )

    assert inserted == 0
    assert chunks_used == 3  # remaining chunks: [4001,6000] [6001,8000] [8001,10000]

    checkpoint_row = await db_session.execute(
        sa.text("SELECT last_processed_block FROM event_indexer_checkpoint WHERE wallet_id = :wid"),
        {"wid": str(wallet_id)},
    )
    assert checkpoint_row.scalar_one() == current_block


@pytest.mark.integration
async def test_chunks_follow_the_readers_narrowed_range_limit(
    db_session: AsyncSession,
) -> None:
    """The indexer chunks by reader.log_chunk_size, not the LOG_CHUNK_SIZE ceiling.

    A provider that caps eth_getLogs at 800 blocks made every 2000-block chunk
    fail outright, so the backfill could never advance (AUD-445).
    """
    wallet_id = await _insert_wallet(db_session, _WALLET_ADDRESS)
    await _insert_checkpoint(db_session, wallet_id, 0)
    await db_session.flush()

    reader = _fake_reader(log_chunk_size=800)

    _, chunks_used = await _index_wallet(
        db_session,
        reader,
        wallet_id=wallet_id,
        wallet_address=_WALLET_ADDRESS,
        token_addresses=[_TOKEN_ADDRESS],
        current_block=2_400,
        max_chunks=10,
    )

    assert chunks_used == 3  # [1,800] [801,1600] [1601,2400]
    spans = {
        (call.kwargs["from_block"], call.kwargs["to_block"])
        for call in reader.get_logs.await_args_list
    }
    assert spans == {(1, 800), (801, 1_600), (1_601, 2_400)}

    checkpoint_row = await db_session.execute(
        sa.text("SELECT last_processed_block FROM event_indexer_checkpoint WHERE wallet_id = :wid"),
        {"wid": str(wallet_id)},
    )
    assert checkpoint_row.scalar_one() == 2_400


@pytest.mark.integration
async def test_zero_budget_defers_without_calling_rpc(db_session: AsyncSession) -> None:
    wallet_id = await _insert_wallet(db_session, _WALLET_ADDRESS)
    await _insert_checkpoint(db_session, wallet_id, 0)
    await db_session.flush()

    reader = _fake_reader()
    inserted, chunks_used = await _index_wallet(
        db_session,
        reader,
        wallet_id=wallet_id,
        wallet_address=_WALLET_ADDRESS,
        token_addresses=[_TOKEN_ADDRESS],
        current_block=LOG_CHUNK_SIZE,
        max_chunks=0,
    )

    assert (inserted, chunks_used) == (0, 0)
    reader.get_logs.assert_not_awaited()
