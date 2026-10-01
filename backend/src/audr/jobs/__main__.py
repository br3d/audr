"""Worker process entrypoint — DISCOVERY, BALANCE_SCAN, QUOTE_REFRESH, EVENT_INDEXER,
NEWS_REFRESH, and on-demand VALIDATE_RPC / VALIDATE_QUOTES job handlers
(AUD-244/AUD-67/AUD-307/AUD-313/AUD-308)."""

from __future__ import annotations

import asyncio
import logging
import signal
import socket
import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audr.assets.catalog import import_catalog
from audr.assets.cmc_catalog import import_cmc_map
from audr.db import _get_session_factory
from audr.jobs.canonicality import recheck_canonicality
from audr.operations.cleanup import cleanup_expired_auth_rows
from audr.jobs.event_indexer import handle_event_indexer
from audr.jobs.news import handle_news_refresh
from audr.jobs.quotes import handle_quote_refresh
from audr.jobs.store import JobKind, upsert_worker_status
from audr.jobs.validation import handle_validate_quotes, handle_validate_rpc
from audr.jobs.worker import Worker
from audr.portfolio.balances import record_balance
from audr.portfolio.discovery import (
    discover_tokens,
    get_discovery_checkpoint,
    persist_discovery_candidates,
    save_discovery_checkpoint,
)
from audr.portfolio.history import materialize_history_point
from audr.portfolio.snapshot import publish_valuation_snapshot
from audr.providers.rpc_reader import RpcReader
from audr.providers.rpc_targets import RpcUrlError, get_validated_rpc_url
from audr.wallets.service import list_wallets

logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = 5.0
_ETH_MAINNET_CHAIN_ID = 1
# Conventional placeholder address for native ETH balance observations.
_ETH_NATIVE_ADDRESS = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"


async def handle_discovery(session: AsyncSession, run_id: uuid.UUID) -> None:
    """Build ERC-20 discovery candidate lists for all active wallets."""
    await _discover_for_active_wallets(session, run_id=run_id)
    await session.commit()


async def _discover_for_active_wallets(session: AsyncSession, *, run_id: uuid.UUID) -> None:
    """Run discovery for every active wallet, keyed per-wallet within the run's checkpoint.

    The run's checkpoint is a single job_run.checkpoint JSON blob shared by all
    wallets in this run, so it must be sub-keyed by wallet address — otherwise
    the "processed" set left behind by wallet N is read back as wallet N+1's
    checkpoint and makes it skip every catalog address already seen.
    """
    wallets = await list_wallets(session)
    run_checkpoint = await get_discovery_checkpoint(session, run_id=run_id) or {}
    for wallet in wallets:
        if wallet.status != "active":
            continue
        result = await discover_tokens(
            session,
            wallet_address=wallet.address,
            use_catalog=True,
            manual_addresses=[],
            checkpoint=run_checkpoint.get(wallet.address),
        )
        if result.checkpoint is not None:
            run_checkpoint[wallet.address] = result.checkpoint
            await save_discovery_checkpoint(
                session,
                run_id=run_id,
                checkpoint=run_checkpoint,
            )
        new_pairs = await persist_discovery_candidates(
            session,
            wallet_address=wallet.address,
            candidates=result.candidates,
        )
        logger.info(
            "discovery run_id=%s wallet=%s candidates=%d new_pairs=%d",
            run_id,
            wallet.address,
            len(result.candidates),
            new_pairs,
        )


async def handle_balance_scan(session: AsyncSession, run_id: uuid.UUID) -> None:
    """Scan ETH and ERC-20 balances for all active wallets at the current block."""
    try:
        rpc_url = await get_validated_rpc_url(session)
    except RpcUrlError:
        logger.exception("balance_scan skipped — RPC URL failed validation run_id=%s", run_id)
        return
    if rpc_url is None:
        logger.warning("balance_scan skipped — no RPC integration configured run_id=%s", run_id)
        return

    wallets = await list_wallets(session)
    active = [w for w in wallets if w.status == "active"]
    if not active:
        return

    rows = await session.execute(
        sa.text(
            """
            SELECT w.address, a.token_address
            FROM monitored_pair mp
            JOIN wallet w ON w.id = mp.wallet_id
            JOIN asset  a ON a.id = mp.asset_id
            WHERE w.status = 'active'
            """
        )
    )
    monitored: dict[str, list[str]] = {}
    for wallet_addr, token_addr in rows:
        monitored.setdefault(wallet_addr, []).append(token_addr)

    async with RpcReader(url=rpc_url, expected_chain_id=_ETH_MAINNET_CHAIN_ID) as rpc:
        await rpc.validate_chain()
        block_number = await rpc.get_block_number()

        for wallet in active:
            addr = wallet.address

            try:
                eth_balance = await rpc.get_eth_balance(addr)
                await record_balance(
                    session,
                    wallet_address=addr,
                    token_address=_ETH_NATIVE_ADDRESS,
                    raw_amount=eth_balance,
                    block_number=block_number,
                )
            except Exception:
                logger.exception("eth balance failed wallet=%s run_id=%s", addr, run_id)

            for token_addr in monitored.get(addr, []):
                try:
                    amount = await rpc.get_erc20_balance(
                        token_address=token_addr,
                        wallet_address=addr,
                    )
                    await record_balance(
                        session,
                        wallet_address=addr,
                        token_address=token_addr,
                        raw_amount=amount,
                        block_number=block_number,
                    )
                except Exception:
                    logger.exception(
                        "erc20 balance failed wallet=%s token=%s run_id=%s",
                        addr,
                        token_addr,
                        run_id,
                    )

    logger.info("balance_scan run_id=%s block=%d", run_id, block_number)
    await session.commit()


async def handle_valuation(session: AsyncSession, run_id: uuid.UUID) -> None:
    """Publish a valuation snapshot and materialize a history point."""
    try:
        result = await publish_valuation_snapshot(session)
    except ValueError as exc:
        logger.info("valuation skipped — %s run_id=%s", exc, run_id)
        return
    await materialize_history_point(session, snapshot_id=result.snapshot_id)
    logger.info(
        "valuation run_id=%s snapshot=%s quality=%s lines=%d",
        run_id,
        result.snapshot_id,
        result.quality,
        result.line_count,
    )
    await session.commit()


async def _record_worker_status(
    factory: async_sessionmaker[AsyncSession], worker_id: str, status: str
) -> None:
    """Write the process liveness heartbeat. Never fatal — a DB blip must not
    kill the worker loop, it only makes /status report a stale heartbeat."""
    try:
        async with factory() as session:
            await upsert_worker_status(session, worker_id=worker_id, status=status)
            await session.commit()
    except Exception:
        logger.warning("worker status heartbeat failed worker_id=%s", worker_id)


async def _bootstrap_catalog(factory: async_sessionmaker[AsyncSession]) -> None:
    """Import the vendored token catalog at startup (idempotent).

    Not fatal to the worker process, but a failure here means ERC-20 discovery
    will find zero candidates — that degradation must stay visible via
    GET /health/ready and GET /api/v1/status (audr.operations.status),
    not just this log line (AUD-357).
    """
    try:
        async with factory() as session:
            version = await import_catalog(session)
            await session.commit()
            logger.info(
                "catalog bootstrap version=%s entries=%d",
                version.commit_hash,
                version.entry_count,
            )
    except Exception:
        logger.error(
            "catalog bootstrap failed — discovery will run without catalog candidates",
            exc_info=True,
        )


async def _bootstrap_cmc_map(factory: async_sessionmaker[AsyncSession]) -> None:
    """Import the vendored CoinMarketCap address/symbol map at startup (idempotent).

    Not fatal to the worker process, but a failure here means quote_refresh's
    keyless CoinMarketCap fallback (AUD-358) will only have pinned majors to
    price with — no address or symbol resolution for the rest of the catalog.
    """
    try:
        async with factory() as session:
            version = await import_cmc_map(session)
            await session.commit()
            logger.info(
                "cmc map bootstrap version=%s entries=%d",
                version.source_hash,
                version.entry_count,
            )
    except Exception:
        logger.error(
            "cmc map bootstrap failed — CoinMarketCap pricing will rely on pins only",
            exc_info=True,
        )


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    factory = _get_session_factory()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stop.set)
    loop.add_signal_handler(signal.SIGINT, stop.set)

    await _bootstrap_catalog(factory)
    await _bootstrap_cmc_map(factory)

    # Hostname, not pid: one worker container per host in this deployment, and a
    # restart must reuse the same worker_status row instead of accumulating one
    # row per process lifetime.
    process_worker_id = socket.gethostname()
    await _record_worker_status(factory, process_worker_id, "idle")

    discovery_worker = Worker(factory, kind=JobKind.DISCOVERY, handler=handle_discovery)
    balance_worker = Worker(factory, kind=JobKind.BALANCE_SCAN, handler=handle_balance_scan)
    quote_worker = Worker(factory, kind=JobKind.QUOTE_REFRESH, handler=handle_quote_refresh)
    event_worker = Worker(factory, kind=JobKind.EVENT_INDEXER, handler=handle_event_indexer)
    news_worker = Worker(factory, kind=JobKind.NEWS_REFRESH, handler=handle_news_refresh)
    # Valuation runs on-demand: enqueued by handle_quote_refresh after each
    # successful quote refresh so snapshots are produced in step with price data.
    valuation_worker = Worker(
        factory, kind=JobKind.VALUATION, handler=handle_valuation, on_demand=True
    )
    # On-demand validation workers — only run when a request is enqueued from
    # the Connections settings page (AUD-313).
    validate_rpc_worker = Worker(
        factory, kind=JobKind.VALIDATE_RPC, handler=handle_validate_rpc, on_demand=True
    )
    validate_quotes_worker = Worker(
        factory,
        kind=JobKind.VALIDATE_QUOTES,
        handler=handle_validate_quotes,
        on_demand=True,
    )

    logger.info("worker started")
    while not stop.is_set():
        try:
            did_work = await discovery_worker.run_once()
            did_work |= await balance_worker.run_once()
            did_work |= await quote_worker.run_once()
            did_work |= await event_worker.run_once()
            did_work |= await news_worker.run_once()
            did_work |= await valuation_worker.run_once()
            did_work |= await validate_rpc_worker.run_once()
            did_work |= await validate_quotes_worker.run_once()
            # Lightweight canonicality sweep — catches any observations
            # invalidated since the last snapshot was published.
            async with factory() as session:
                await recheck_canonicality(session)
                await session.commit()
            # Housekeeping: login_attempt/session rows are never pruned
            # otherwise, so both tables grow without bound (AUD-322).
            async with factory() as session:
                deleted = await cleanup_expired_auth_rows(session)
                await session.commit()
                if deleted["login_attempt"] or deleted["session"]:
                    logger.info("auth cleanup deleted=%s", deleted)
        except Exception:
            logger.exception("worker poll error")
            did_work = False

        # Liveness heartbeat: GET /api/v1/status reads the newest worker_status
        # row and reports "unknown" when the table is empty (AUD-318).
        await _record_worker_status(
            factory, process_worker_id, "running" if did_work else "idle"
        )

        if not did_work:
            try:
                await asyncio.wait_for(stop.wait(), timeout=_POLL_INTERVAL_S)
            except asyncio.TimeoutError:
                pass

    await _record_worker_status(factory, process_worker_id, "stopped")
    logger.info("worker stopped")


if __name__ == "__main__":
    asyncio.run(_main())
