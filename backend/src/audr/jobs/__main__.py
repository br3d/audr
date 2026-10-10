"""Worker process entrypoint — DISCOVERY, BALANCE_SCAN, QUOTE_REFRESH, EVENT_INDEXER,
NEWS_REFRESH, ASSET_ICON_REFRESH, and on-demand VALIDATE_RPC / VALIDATE_QUOTES job
handlers (AUD-244/AUD-67/AUD-307/AUD-313/AUD-308/AUD-385)."""

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
from audr.assets.constants import NATIVE_ETH_ADDRESS
from audr.db import _get_session_factory
from audr.jobs.asset_icons import handle_asset_icon_refresh
from audr.jobs.canonicality import recheck_canonicality
from audr.jobs.event_indexer import handle_event_indexer
from audr.jobs.news import handle_news_refresh
from audr.jobs.policy import RetryPolicy, get_shared_rpc_rate_limiter
from audr.jobs.quotes import handle_quote_refresh
from audr.jobs.store import (
    JobKind,
    enqueue_job,
    fail_job,
    get_job_params,
    set_checkpoint,
    upsert_worker_status,
)
from audr.jobs.validation import handle_validate_quotes, handle_validate_rpc
from audr.jobs.worker import Worker
from audr.operations.cleanup import cleanup_expired_auth_rows
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
from audr.providers.rpc_targets import RpcUrlError, get_rpc_endpoints
from audr.wallets.models import Wallet
from audr.wallets.service import get_wallet, list_wallets

logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = 5.0
_ETH_MAINNET_CHAIN_ID = 1

# AUD-463: a balance read that fails is retried a couple of times with
# backoff before counting against the run. The -32603 bursts seen in
# production recovered across consecutive hourly runs under the same token
# load (300 -> 22 -> 14 failed reads) — the signature of provider-side
# throttling, not of individual dead token contracts — so a short in-run
# retry recovers most of them instead of losing the whole hour's read.
_BALANCE_SCAN_RETRY_POLICY = RetryPolicy(max_attempts=2, base_delay_s=2.0, max_delay_s=20.0)

# AUD-463: per-token tolerance stays — one bad token must never fail the
# whole scan — but a run that loses more than this fraction of its attempted
# reads (after retries) must not land indistinguishable from a clean
# completion. Chosen comfortably above the ~3-5% noise observed in healthy
# runs and well below the ~74% loss of the run that triggered this ticket.
_BALANCE_SCAN_DEGRADED_FAILURE_RATIO = 0.2


async def _scoped_wallet_id(session: AsyncSession, *, run_id: uuid.UUID) -> uuid.UUID | None:
    """Return the wallet this run is scoped to, or None for "all active wallets".

    AUD-399/AUD-400: a per-wallet "Refresh balances" / "Discover tokens" request
    carries ``params = {"wallet_id": ...}`` on its job_run row. No params (the
    scheduled-run case) means no scope — callers must fall back to iterating
    every active wallet, unchanged from before this feature.
    """
    params = await get_job_params(session, run_id=run_id)
    if not params or params.get("wallet_id") is None:
        return None
    return uuid.UUID(params["wallet_id"])


async def _resolve_scoped_wallet(session: AsyncSession, *, wallet_id: uuid.UUID) -> Wallet | None:
    """Return the scoped wallet if it still exists and is active, else None."""
    wallet = await get_wallet(session, wallet_id=wallet_id)
    if wallet is None or wallet.status != "active":
        return None
    return wallet


async def handle_discovery(session: AsyncSession, run_id: uuid.UUID) -> None:
    """Build ERC-20 discovery candidate lists for all active wallets."""
    wallet_id = await _scoped_wallet_id(session, run_id=run_id)
    await _discover_for_active_wallets(session, run_id=run_id, wallet_id=wallet_id)
    await session.commit()


async def _discover_for_active_wallets(
    session: AsyncSession, *, run_id: uuid.UUID, wallet_id: uuid.UUID | None = None
) -> None:
    """Run discovery for every active wallet, keyed per-wallet within the run's checkpoint.

    The run's checkpoint is a single job_run.checkpoint JSON blob shared by all
    wallets in this run, so it must be sub-keyed by wallet address — otherwise
    the "processed" set left behind by wallet N is read back as wallet N+1's
    checkpoint and makes it skip every catalog address already seen.

    ``wallet_id`` (AUD-399/AUD-400) restricts this to a single wallet instead
    of every active one. If that wallet no longer exists or is no longer
    active, this logs and returns without scanning anything else — falling
    back to "all active wallets" would be exactly the RPC blow-up a scoped
    request exists to avoid.
    """
    if wallet_id is not None:
        wallet = await _resolve_scoped_wallet(session, wallet_id=wallet_id)
        if wallet is None:
            logger.info(
                "discovery skipped — scoped wallet %s not found or inactive run_id=%s",
                wallet_id,
                run_id,
            )
            return
        wallets = [wallet]
    else:
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
    """Scan ETH and ERC-20 balances for all active wallets at the current block.

    ``wallet_id`` (AUD-399/AUD-400) restricts this to a single wallet instead
    of every active one — the wallet-card "Refresh balances" action, so one
    address can be refreshed without burning RPC calls on every tracked
    wallet. If that wallet no longer exists or is no longer active, this logs
    and returns without making any RPC calls at all — falling back to "all
    active wallets" would be exactly the RPC blow-up a scoped request exists
    to avoid.
    """
    wallet_id = await _scoped_wallet_id(session, run_id=run_id)
    if wallet_id is not None:
        scoped_wallet = await _resolve_scoped_wallet(session, wallet_id=wallet_id)
        if scoped_wallet is None:
            logger.info(
                "balance_scan skipped — scoped wallet %s not found or inactive run_id=%s",
                wallet_id,
                run_id,
            )
            return

    try:
        rpc_endpoints = await get_rpc_endpoints(session)
    except RpcUrlError:
        logger.exception("balance_scan skipped — RPC URL failed validation run_id=%s", run_id)
        return

    if wallet_id is not None:
        active = [scoped_wallet]
    else:
        wallets = await list_wallets(session)
        active = [w for w in wallets if w.status == "active"]
        if not active:
            return

    # Keyed by wallet_id rather than address: monitored_pair already carries
    # wallet_id directly, so no join against wallet (and no address
    # decryption) is needed just to find each active wallet's monitored
    # tokens (AUD-490).
    monitor_stmt = sa.text(
        "SELECT mp.wallet_id, a.token_address"
        " FROM monitored_pair mp"
        " JOIN asset a ON a.id = mp.asset_id"
        " WHERE mp.wallet_id IN :wallet_ids"
    ).bindparams(sa.bindparam("wallet_ids", expanding=True))
    rows = await session.execute(
        monitor_stmt, {"wallet_ids": [str(w.id) for w in active]}
    )
    monitored: dict[uuid.UUID, list[str]] = {}
    for row_wallet_id, token_addr in rows:
        monitored.setdefault(uuid.UUID(str(row_wallet_id)), []).append(token_addr)

    async with RpcReader(
        url=rpc_endpoints[0],
        fallback_urls=rpc_endpoints[1:],
        expected_chain_id=_ETH_MAINNET_CHAIN_ID,
        rate_limiter=get_shared_rpc_rate_limiter(),
    ) as rpc:
        await rpc.validate_chain()
        block_number = await rpc.get_block_number()
        try:
            block_time = await rpc.get_block_time(block_number)
        except Exception:
            # Freshness reporting degrades to block-number-only; the scan itself
            # must not fail just because the timestamp call didn't land.
            logger.exception("block_time fetch failed block=%d run_id=%s", block_number, run_id)
            block_time = None

        attempted = 0
        # (wallet_address, token_address) pending a retry; token_address is
        # None for the native ETH read.
        failures: list[tuple[str, str | None]] = []

        async def _read_and_record(addr: str, token_addr: str | None) -> bool:
            try:
                if token_addr is None:
                    raw_amount = await rpc.get_eth_balance(addr)
                    resolved_token = NATIVE_ETH_ADDRESS
                else:
                    raw_amount = await rpc.get_erc20_balance(
                        token_address=token_addr,
                        wallet_address=addr,
                    )
                    resolved_token = token_addr
            except Exception as exc:
                # A read that the retry loop below recovers is not an operator
                # problem, so it stays a one-line warning with no traceback —
                # see the ERROR emitted after the retry budget runs out. The
                # stand showed why this matters: 66 of these for a run that
                # ended `failed=0`, which made grepping the old 'balance
                # failed' ERROR lines actively misleading.
                logger.warning(
                    "balance read failed (will retry) wallet=%s token=%s run_id=%s: %s",
                    addr,
                    token_addr or "eth",
                    run_id,
                    exc,
                )
                return False
            await record_balance(
                session,
                wallet_address=addr,
                token_address=resolved_token,
                raw_amount=raw_amount,
                block_number=block_number,
                block_time=block_time,
            )
            return True

        for wallet in active:
            addr = wallet.address

            attempted += 1
            if not await _read_and_record(addr, None):
                failures.append((addr, None))

            for token_addr in monitored.get(wallet.id, []):
                attempted += 1
                if not await _read_and_record(addr, token_addr):
                    failures.append((addr, token_addr))

        # AUD-463: give the failed subset a few backed-off retries before
        # writing them off for the hour — see _BALANCE_SCAN_RETRY_POLICY.
        # Per-token tolerance stays: a read still failing after retries does
        # not raise, it just stays counted as failed.
        retry_attempt = 0
        while failures and _BALANCE_SCAN_RETRY_POLICY.is_retryable(retry_attempt):
            await asyncio.sleep(_BALANCE_SCAN_RETRY_POLICY.delay_for(retry_attempt))
            retry_attempt += 1
            still_failing = [
                (addr, token_addr)
                for addr, token_addr in failures
                if not await _read_and_record(addr, token_addr)
            ]
            failures = still_failing

        # Only now is a read genuinely lost for the hour. One ERROR per lost
        # read, so `grep -c 'balance read lost'` counts exactly what the
        # checkpoint's `failed` reports and nothing that recovered.
        for addr, token_addr in failures:
            logger.error(
                "balance read lost after %d retries wallet=%s token=%s run_id=%s",
                retry_attempt,
                addr,
                token_addr or "eth",
                run_id,
            )

    failed = len(failures)

    # Republish the portfolio off the balances we just read (AUD-446). The
    # dashboard renders the latest valuation_snapshot, not balance_observation,
    # so without this a "Refresh balances" click updated the observations and
    # changed nothing the user could see until the next hourly quote_refresh
    # happened to enqueue a valuation of its own — the button looked broken.
    # handle_valuation no-ops with a log line when there are no prices yet.
    await enqueue_job(session, kind=JobKind.VALUATION)

    logger.info(
        "balance_scan run_id=%s block=%d attempted=%d failed=%d",
        run_id,
        block_number,
        attempted,
        failed,
    )

    checkpoint = {"attempted": attempted, "failed": failed}
    ratio = failed / attempted if attempted else 0.0
    if attempted and ratio > _BALANCE_SCAN_DEGRADED_FAILURE_RATIO:
        # AUD-463: one bad token must never fail the whole scan, but losing
        # most of a run's reads to provider throttling is not a "completed"
        # run — flip it to `failed` so the existing retry-backoff budget
        # (jobs/store.claim_job) picks it back up, instead of this looking
        # identical to a fully clean hour.
        await fail_job(
            session,
            run_id=run_id,
            error=(
                f"{failed}/{attempted} balance reads failed after retries ({ratio:.0%}) "
                f"— see 'eth/erc20 balance failed' log lines for run_id={run_id}"
            ),
            checkpoint=checkpoint,
        )
    else:
        await set_checkpoint(session, run_id=run_id, checkpoint=checkpoint)

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
    icon_worker = Worker(
        factory, kind=JobKind.ASSET_ICON_REFRESH, handler=handle_asset_icon_refresh
    )
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
            did_work |= await icon_worker.run_once()
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
        await _record_worker_status(factory, process_worker_id, "running" if did_work else "idle")

        if not did_work:
            try:
                await asyncio.wait_for(stop.wait(), timeout=_POLL_INTERVAL_S)
            except TimeoutError:
                pass

    await _record_worker_status(factory, process_worker_id, "stopped")
    logger.info("worker stopped")


if __name__ == "__main__":
    asyncio.run(_main())
