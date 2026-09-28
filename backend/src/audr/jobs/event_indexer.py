"""Event indexer job — incremental ERC-20 Transfer indexing per wallet (AUD-307).

Algorithm per run:
  1. Load all active wallets.
  2. Load all tracked token addresses (from asset table).
  3. For each wallet, read its checkpoint (last_processed_block).
     If no checkpoint, start from current_block (no backfill).
  4. For each wallet, fetch Transfer events from checkpoint+1 to current_block
     in LOG_CHUNK_SIZE chunks, alternating direction filters:
       - logs where wallet is Transfer sender (topics[1] = wallet)
       - logs where wallet is Transfer receiver (topics[2] = wallet)
  5. Insert events into onchain_event (ON CONFLICT DO NOTHING).
  6. Advance checkpoint to current_block.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from audr.providers.rpc_reader import (
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
from audr.settings.integrations import get_integration

logger = logging.getLogger(__name__)

_MAINNET_CHAIN_ID = 1


async def handle_event_indexer(session: AsyncSession, run_id: uuid.UUID) -> None:
    """Index ERC-20 Transfer events for all active wallets."""
    integration = await get_integration(session, kind="rpc", decrypt_fields=True)
    if integration is None or not integration.url:
        logger.warning("event_indexer skipped — no RPC URL configured run_id=%s", run_id)
        return
    rpc_url = integration.url

    token_addresses = await _get_tracked_token_addresses(session)
    if not token_addresses:
        logger.info("event_indexer skipped — no tracked tokens run_id=%s", run_id)
        return

    wallets = await _get_active_wallets(session)
    if not wallets:
        logger.info("event_indexer skipped — no active wallets run_id=%s", run_id)
        return

    async with RpcReader(url=rpc_url, expected_chain_id=_MAINNET_CHAIN_ID) as reader:
        current_block = await reader.get_block_number()
        logger.info(
            "event_indexer run_id=%s current_block=%d wallets=%d tokens=%d",
            run_id,
            current_block,
            len(wallets),
            len(token_addresses),
        )

        total_events = 0
        for wallet_id, wallet_address in wallets:
            indexed = await _index_wallet(
                session,
                reader,
                wallet_id=wallet_id,
                wallet_address=wallet_address,
                token_addresses=token_addresses,
                current_block=current_block,
            )
            total_events += indexed

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
) -> int:
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
        return 0

    from_block = checkpoint + 1
    if from_block > current_block:
        return 0  # already up to date

    wallet_topic = _pad_address_topic(wallet_address)
    inserted = 0

    # Process in LOG_CHUNK_SIZE block chunks
    chunk_start = from_block
    while chunk_start <= current_block:
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
            return inserted

        seen: set[tuple[str, int]] = set()
        for log in [*out_logs, *in_logs]:
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

    await _upsert_checkpoint(session, wallet_id, current_block)
    await session.flush()
    logger.info(
        "event_indexer wallet=%s blocks=%d-%d inserted=%d",
        wallet_address,
        from_block,
        current_block,
        inserted,
    )
    return inserted


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
        amount = decode_transfer_amount(log)
    except MalformedResponseError:
        logger.warning("event_indexer malformed amount tx=%s idx=%d", log.tx_hash, log.log_index)
        return 0

    from_addr = _topic_to_address(log.topics[1])
    to_addr = _topic_to_address(log.topics[2])
    wallet_norm = wallet_address.lower()

    if from_addr == wallet_norm:
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
    rows = await session.execute(
        sa.text("SELECT id, address FROM wallet WHERE status = 'active'")
    )
    return [(uuid.UUID(str(row[0])), str(row[1])) for row in rows.fetchall()]


async def _get_tracked_token_addresses(session: AsyncSession) -> list[str]:
    rows = await session.execute(
        sa.text("SELECT token_address FROM asset WHERE excluded = false")
    )
    return [str(row[0]) for row in rows.fetchall()]


async def _get_checkpoint(
    session: AsyncSession, wallet_id: uuid.UUID
) -> int | None:
    row = await session.execute(
        sa.text(
            "SELECT last_processed_block FROM event_indexer_checkpoint WHERE wallet_id = :wid"
        ),
        {"wid": str(wallet_id)},
    )
    result = row.first()
    return int(result[0]) if result else None


async def _upsert_checkpoint(
    session: AsyncSession, wallet_id: uuid.UUID, block: int
) -> None:
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


