"""Event indexer job — incremental ERC-20 Transfer + Approval indexing per wallet
(AUD-307, AUD-300).

Algorithm per run:
  1. Load all active wallets.
  2. Load all tracked token addresses (from asset table).
  3. For each wallet, read its checkpoint (last_processed_block).
     If no checkpoint, start from current_block (no backfill).
  4. For each wallet, fetch events from checkpoint+1 to current_block in
     LOG_CHUNK_SIZE chunks:
       - Transfer logs where wallet is sender (topics[1] = wallet)
       - Transfer logs where wallet is receiver (topics[2] = wallet)
       - Approval logs where wallet is owner (topics[1] = wallet) — feeds the
         allowance/security-signals view (GET /api/v1/allowances)
  5. Insert events into onchain_event (ON CONFLICT DO NOTHING).
  6. Advance checkpoint to current_block.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from audr.config import get_settings
from audr.jobs.policy import get_shared_rpc_rate_limiter
from audr.providers.rpc_reader import (
    APPROVAL_TOPIC,
    LOG_CHUNK_SIZE,
    TRANSFER_TOPIC,
    LogEntry,
    MalformedResponseError,
    RpcError,
    RpcReader,
    _pad_address_topic,
    _topic_to_address,
    decode_transfer_amount,
)
from audr.providers.rpc_targets import RpcUrlError, get_rpc_endpoints

logger = logging.getLogger(__name__)

_MAINNET_CHAIN_ID = 1


async def handle_event_indexer(session: AsyncSession, run_id: uuid.UUID) -> None:
    """Index ERC-20 Transfer events for all active wallets."""
    try:
        rpc_endpoints = await get_rpc_endpoints(session)
    except RpcUrlError:
        logger.exception("event_indexer skipped — RPC URL failed validation run_id=%s", run_id)
        return

    token_addresses = await _get_tracked_token_addresses(session)
    if not token_addresses:
        logger.info("event_indexer skipped — no tracked tokens run_id=%s", run_id)
        return

    wallets = await _get_active_wallets(session)
    if not wallets:
        logger.info("event_indexer skipped — no active wallets run_id=%s", run_id)
        return

    max_chunks = get_settings().event_indexer_max_chunks_per_run

    async with RpcReader(
        url=rpc_endpoints[0],
        fallback_urls=rpc_endpoints[1:],
        expected_chain_id=_MAINNET_CHAIN_ID,
        rate_limiter=get_shared_rpc_rate_limiter(),
    ) as reader:
        current_block = await reader.get_block_number()
        logger.info(
            "event_indexer run_id=%s current_block=%d wallets=%d tokens=%d chunk_budget=%d",
            run_id,
            current_block,
            len(wallets),
            len(token_addresses),
            max_chunks,
        )

        total_events = 0
        chunks_remaining = max_chunks
        deferred_wallets = 0
        for wallet_id, wallet_address in wallets:
            if chunks_remaining <= 0:
                deferred_wallets += 1
                continue
            indexed, chunks_used = await _index_wallet(
                session,
                reader,
                wallet_id=wallet_id,
                wallet_address=wallet_address,
                token_addresses=token_addresses,
                current_block=current_block,
                max_chunks=chunks_remaining,
            )
            total_events += indexed
            chunks_remaining -= chunks_used

    if deferred_wallets:
        logger.info(
            "event_indexer run_id=%s chunk budget exhausted — deferred wallets=%d to next run",
            run_id,
            deferred_wallets,
        )

    logger.info(
        "event_indexer run_id=%s done total_events_inserted=%d",
        run_id,
        total_events,
    )


# ---------------------------------------------------------------------------
# Per-wallet indexing
# ---------------------------------------------------------------------------


async def _index_wallet(
    session: AsyncSession,
    reader: RpcReader,
    *,
    wallet_id: uuid.UUID,
    wallet_address: str,
    token_addresses: list[str],
    current_block: int,
    max_chunks: int,
) -> tuple[int, int]:
    """Index one wallet's events; returns (events_inserted, chunks_used).

    ``max_chunks`` bounds how many LOG_CHUNK_SIZE block-ranges this call may
    process — once exhausted, progress is checkpointed at the last completed
    chunk boundary and the remaining range resumes on the wallet's next run
    (AUD-362), so a long catch-up range can't burn the whole run's RPC budget.
    """
    checkpoint = await _get_checkpoint(session, wallet_id)
    if checkpoint is None:
        # First run: record current block and index nothing (no backfill)
        await _upsert_checkpoint(session, wallet_id, current_block)
        await session.flush()
        logger.info(
            "event_indexer wallet=%s checkpoint initialised at block=%d",
            wallet_address,
            current_block,
        )
        return 0, 0

    from_block = checkpoint + 1
    if from_block > current_block or max_chunks <= 0:
        return 0, 0  # already up to date, or no budget left this run

    wallet_topic = _pad_address_topic(wallet_address)
    inserted = 0
    chunks_used = 0

    # Process in LOG_CHUNK_SIZE block chunks
    chunk_start = from_block
    while chunk_start <= current_block:
        if chunks_used >= max_chunks:
            await _upsert_checkpoint(session, wallet_id, chunk_start - 1)
            await session.flush()
            logger.info(
                "event_indexer wallet=%s chunk budget exhausted at block=%d, resuming next run",
                wallet_address,
                chunk_start - 1,
            )
            return inserted, chunks_used

        chunk_end = min(chunk_start + LOG_CHUNK_SIZE - 1, current_block)

        try:
            # Outbound transfers (wallet is sender — topic[1])
            out_logs = await reader.get_logs(
                from_block=chunk_start,
                to_block=chunk_end,
                address=token_addresses,
                topics=[TRANSFER_TOPIC, wallet_topic],
            )
            # Inbound transfers (wallet is receiver — topic[2])
            in_logs = await reader.get_logs(
                from_block=chunk_start,
                to_block=chunk_end,
                address=token_addresses,
                topics=[TRANSFER_TOPIC, None, wallet_topic],
            )
            # Approvals granted by the wallet, as owner — topic[1]
            approval_logs = await reader.get_logs(
                from_block=chunk_start,
                to_block=chunk_end,
                address=token_addresses,
                topics=[APPROVAL_TOPIC, wallet_topic],
            )
        except (RpcError, MalformedResponseError):
            logger.exception(
                "event_indexer wallet=%s chunk %d-%d failed",
                wallet_address,
                chunk_start,
                chunk_end,
            )
            # Update checkpoint to last successful chunk boundary
            if chunk_start > from_block:
                await _upsert_checkpoint(session, wallet_id, chunk_start - 1)
                await session.flush()
            return inserted, chunks_used

        seen: set[tuple[str, int]] = set()
        for log in [*out_logs, *in_logs, *approval_logs]:
            key = (log.tx_hash.lower(), log.log_index)
            if key in seen:
                continue
            seen.add(key)

            try:
                n = await _insert_event(
                    session,
                    log=log,
                    wallet_id=wallet_id,
                    wallet_address=wallet_address,
                )
                inserted += n
            except Exception:
                logger.exception(
                    "event_indexer wallet=%s failed to insert log tx=%s idx=%d",
                    wallet_address,
                    log.tx_hash,
                    log.log_index,
                )

        chunk_start = chunk_end + 1
        chunks_used += 1

    await _upsert_checkpoint(session, wallet_id, current_block)
    await session.flush()
    logger.info(
        "event_indexer wallet=%s blocks=%d-%d inserted=%d",
        wallet_address,
        from_block,
        current_block,
        inserted,
    )
    return inserted, chunks_used


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


async def _insert_event(
    session: AsyncSession,
    *,
    log: LogEntry,
    wallet_id: uuid.UUID,
    wallet_address: str,
) -> int:
    """Insert one event row; return 1 if inserted, 0 if duplicate."""
    if len(log.topics) < 3:
        logger.debug("event_indexer skipping log with fewer than 3 topics tx=%s", log.tx_hash)
        return 0

    try:
        # Both Transfer and Approval encode a single uint256 in `data`.
        amount = decode_transfer_amount(log)
    except MalformedResponseError:
        logger.warning("event_indexer malformed amount tx=%s idx=%d", log.tx_hash, log.log_index)
        return 0

    from_addr = _topic_to_address(log.topics[1])
    to_addr = _topic_to_address(log.topics[2])
    wallet_norm = wallet_address.lower()

    if log.topics[0] == APPROVAL_TOPIC:
        # Approval(owner indexed, spender indexed, value) — from_addr=owner,
        # to_addr=spender. Our topic filter only asks for owner == wallet.
        if from_addr != wallet_norm:
            return 0
        event_type = "approval"
    elif from_addr == wallet_norm:
        event_type = "transfer_out"
    elif to_addr == wallet_norm:
        event_type = "transfer_in"
    else:
        # Shouldn't happen given our topic filter, but be defensive
        return 0

    result = await session.execute(
        sa.text(
            """
            INSERT INTO onchain_event
              (wallet_id, tx_hash, block_number, log_index, event_type,
               token_address, from_address, to_address, raw_amount)
            VALUES
              (:wallet_id, :tx_hash, :block_number, :log_index, :event_type,
               :token_address, :from_address, :to_address, :raw_amount)
            ON CONFLICT (tx_hash, log_index) DO NOTHING
            """
        ),
        {
            "wallet_id": str(wallet_id),
            "tx_hash": log.tx_hash.lower(),
            "block_number": log.block_number,
            "log_index": log.log_index,
            "event_type": event_type,
            "token_address": log.address,
            "from_address": from_addr,
            "to_address": to_addr,
            "raw_amount": str(amount),
        },
    )
    return result.rowcount


async def _get_active_wallets(
    session: AsyncSession,
) -> list[tuple[uuid.UUID, str]]:
    rows = await session.execute(sa.text("SELECT id, address FROM wallet WHERE status = 'active'"))
    return [(uuid.UUID(str(row[0])), str(row[1])) for row in rows.fetchall()]


async def _get_tracked_token_addresses(session: AsyncSession) -> list[str]:
    rows = await session.execute(sa.text("SELECT token_address FROM asset WHERE excluded = false"))
    return [str(row[0]) for row in rows.fetchall()]


async def _get_checkpoint(session: AsyncSession, wallet_id: uuid.UUID) -> int | None:
    row = await session.execute(
        sa.text("SELECT last_processed_block FROM event_indexer_checkpoint WHERE wallet_id = :wid"),
        {"wid": str(wallet_id)},
    )
    result = row.first()
    return int(result[0]) if result else None


async def _upsert_checkpoint(session: AsyncSession, wallet_id: uuid.UUID, block: int) -> None:
    await session.execute(
        sa.text(
            """
            INSERT INTO event_indexer_checkpoint (wallet_id, last_processed_block)
            VALUES (:wid, :block)
            ON CONFLICT (wallet_id)
            DO UPDATE SET last_processed_block = :block, updated_at = now()
            """
        ),
        {"wid": str(wallet_id), "block": block},
    )
