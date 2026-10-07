"""handle_balance_scan failure accounting (AUD-463).

A `balance_scan` run tolerates individual bad token reads — one flaky token
must never fail the whole scan — but it must stop reporting `completed`
indistinguishably from a fully clean run when most of its reads failed.
Before this fix, every exception inside the per-token loop was logged and
swallowed with no counter, so `job_run` recorded `status=completed,
retry_count=0, error=NULL` whether 0 or 300 of 408 token reads failed.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audr.jobs import __main__ as worker_main
from audr.jobs.policy import RetryPolicy
from audr.jobs.store import (
    JobKind,
    JobRunStatus,
    claim_pending_job,
    complete_job,
    enqueue_job,
    get_job_run,
)
from audr.wallets.service import add_wallet

_WALLET_A = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
_TOKEN_GOOD = "0x1111111111111111111111111111111111111111"
_TOKEN_BAD = "0x2222222222222222222222222222222222222222"
_TOKEN_RECOVERS = "0x3333333333333333333333333333333333333333"

_CLEAN_ORDER = (
    "balance_observation",
    "discovery_coverage",
    "monitored_pair",
    "job_run",
    "wallet",
    "asset",
)


async def _wipe(db_session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with db_session_factory() as session:
        async with session.begin():
            for tbl in _CLEAN_ORDER:
                await session.execute(text(f"DELETE FROM {tbl}"))  # noqa: S608 — tbl is from the hardcoded _CLEAN_ORDER tuple above, not user input
            # A run that lands `completed` advances schedule.last_run_at for its
            # kind (store.complete_job) — a real committed side effect on a row
            # this module doesn't own. Left alone, it stalls claim_job's freshness
            # gate for every other BALANCE_SCAN test in the suite for up to an
            # hour after this file runs (seen as spurious failures in
            # test_job_retry_backoff.py / test_restart.py). Reset it to the
            # migration-seeded default (NULL) rather than deleting the row.
            await session.execute(
                text(
                    "UPDATE schedule SET last_run_at = NULL, next_run_at = NULL"
                    " WHERE kind = 'balance_scan'"
                )
            )


@pytest.fixture(autouse=True)
async def _clean_tables(db_session_factory: async_sessionmaker[AsyncSession]) -> None:
    await _wipe(db_session_factory)
    yield
    await _wipe(db_session_factory)


class _ScriptedRpcReader:
    """A fake RpcReader whose get_erc20_balance outcome is scripted per token.

    ``scripts[token_address]`` is a list of outcomes consumed in order, one
    per call attempt for that token — an Exception instance to raise, or
    None to succeed. A token with no script, or an exhausted script, always
    succeeds.
    """

    def __init__(self, **_kwargs: Any) -> None:
        self.scripts: dict[str, list[Exception | None]] = {}
        self.eth_calls: list[str] = []
        self.erc20_calls: list[tuple[str, str]] = []

    async def __aenter__(self) -> _ScriptedRpcReader:
        return self

    async def __aexit__(self, *_exc_info: object) -> bool:
        return False

    async def validate_chain(self) -> None:
        return None

    async def get_block_number(self) -> int:
        return 100

    async def get_block_time(self, _block_number: int) -> None:
        return None

    async def get_eth_balance(self, address: str) -> int:
        self.eth_calls.append(address)
        return 10**18

    async def get_erc20_balance(self, *, token_address: str, wallet_address: str) -> int:
        self.erc20_calls.append((wallet_address, token_address))
        script = self.scripts.get(token_address)
        if script:
            outcome = script.pop(0)
            if outcome is not None:
                raise outcome
        return 42


@pytest.fixture()
def scripted_rpc_reader(monkeypatch: pytest.MonkeyPatch) -> _ScriptedRpcReader:
    reader = _ScriptedRpcReader()
    monkeypatch.setattr(worker_main, "RpcReader", lambda **_kwargs: reader)
    # Fast, deterministic retries — production backoff would make this test slow.
    monkeypatch.setattr(
        worker_main,
        "_BALANCE_SCAN_RETRY_POLICY",
        RetryPolicy(max_attempts=2, base_delay_s=0.0, max_delay_s=0.0, jitter=False),
    )
    return reader


async def _add_monitored_pair(
    db_session_factory: async_sessionmaker[AsyncSession], *, wallet_id: Any, token_address: str
) -> None:
    async with db_session_factory() as session:
        async with session.begin():
            asset_id = (
                await session.execute(
                    text(
                        "INSERT INTO asset (token_address, symbol, name, decimals, source)"
                        " VALUES (:addr, 'TKN', 'Token', 18, 'manual')"
                        " RETURNING id"
                    ),
                    {"addr": token_address},
                )
            ).scalar_one()
            await session.execute(
                text("INSERT INTO monitored_pair (wallet_id, asset_id) VALUES (:wid, :aid)"),
                {"wid": wallet_id, "aid": asset_id},
            )


async def _latest_run_id(db_session_factory: async_sessionmaker[AsyncSession]) -> Any:
    async with db_session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT id FROM job_run WHERE kind = 'balance_scan'"
                    " ORDER BY created_at DESC LIMIT 1"
                )
            )
        ).first()
        assert row is not None
        return row[0]


async def _observed_tokens(
    db_session_factory: async_sessionmaker[AsyncSession], *, wallet_id: Any
) -> set[str]:
    async with db_session_factory() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT a.token_address FROM balance_observation bo"
                        " JOIN asset a ON a.id = bo.asset_id"
                        " WHERE bo.wallet_id = :wid"
                    ),
                    {"wid": wallet_id},
                )
            )
            .scalars()
            .all()
        )
    return set(rows)


async def _run_balance_scan(
    db_session_factory: async_sessionmaker[AsyncSession], *, run_id: Any
) -> None:
    """Drive *run_id* through handle_balance_scan the way the real Worker does.

    Production's Worker._execute_with_heartbeat claims a pending run to
    `in_progress` before invoking the handler, then unconditionally calls
    complete_job afterwards (store.complete_job's `WHERE status =
    'in_progress'` guard makes that a no-op if the handler already flipped
    the run to `failed` itself). Calling handle_balance_scan directly on a
    still-`pending` run — skipping both steps — left fail_job/complete_job's
    same guard silently matching nothing, so the run never left `pending`
    regardless of what the handler decided.
    """
    async with db_session_factory() as session:
        claimed = await claim_pending_job(session, kind=JobKind.BALANCE_SCAN)
        assert claimed == run_id
        await session.commit()

    async with db_session_factory() as session:
        await worker_main.handle_balance_scan(session, run_id)
        await complete_job(session, run_id=run_id)
        await session.commit()


@pytest.mark.integration
async def test_partial_token_failures_below_threshold_still_write_good_observations(
    db_session_factory: async_sessionmaker[AsyncSession],
    scripted_rpc_reader: _ScriptedRpcReader,
    test_secret_key: str,
) -> None:
    """One bad token among many good ones must not fail the scan, but the
    run must carry a record of what failed instead of looking fully clean."""
    async with db_session_factory() as session:
        wallet = await add_wallet(session, address=_WALLET_A, label="A")
        await session.commit()

    good_tokens = [f"0x{i:040x}" for i in range(0x10, 0x19)]  # 9 good tokens
    for token in (*good_tokens, _TOKEN_BAD):
        await _add_monitored_pair(db_session_factory, wallet_id=wallet.id, token_address=token)

    scripted_rpc_reader.scripts[_TOKEN_BAD] = [RuntimeError("boom")] * 5  # always fails

    async with db_session_factory() as session:
        run_id = await enqueue_job(session, kind=JobKind.BALANCE_SCAN)
        await session.commit()

    await _run_balance_scan(db_session_factory, run_id=run_id)

    observed = await _observed_tokens(db_session_factory, wallet_id=wallet.id)
    assert set(good_tokens) <= observed, "every good token's read must still be recorded"
    assert _TOKEN_BAD not in observed, "a token that never succeeded has no observation to carry"

    async with db_session_factory() as session:
        run = await get_job_run(session, run_id=run_id)
    assert run is not None
    assert run.status == JobRunStatus.COMPLETED, (
        "one bad token out of ten must not fail the whole scan"
    )
    # 1 ETH read + 10 token reads attempted; only the one scripted token failed.
    assert run.checkpoint == {"attempted": 11, "failed": 1}, (
        "the run must record how many of its reads failed, not just whether any did"
    )


@pytest.mark.integration
async def test_high_failure_ratio_marks_the_run_failed_not_completed(
    db_session_factory: async_sessionmaker[AsyncSession],
    scripted_rpc_reader: _ScriptedRpcReader,
    test_secret_key: str,
) -> None:
    """Losing most reads for a run must not be indistinguishable from success —
    this is the AUD-463 bug: `status=completed, retry_count=0, error=NULL` for
    a run that actually lost 300 of 408 token reads."""
    async with db_session_factory() as session:
        wallet = await add_wallet(session, address=_WALLET_A, label="A")
        await session.commit()

    bad_tokens = [f"0x{i:040x}" for i in range(1, 9)]
    for token in bad_tokens:
        await _add_monitored_pair(db_session_factory, wallet_id=wallet.id, token_address=token)
        scripted_rpc_reader.scripts[token] = [RuntimeError("boom")] * 5

    await _add_monitored_pair(db_session_factory, wallet_id=wallet.id, token_address=_TOKEN_GOOD)

    async with db_session_factory() as session:
        run_id = await enqueue_job(session, kind=JobKind.BALANCE_SCAN)
        await session.commit()

    await _run_balance_scan(db_session_factory, run_id=run_id)

    # The good read must still land even though the run as a whole is flagged.
    observed = await _observed_tokens(db_session_factory, wallet_id=wallet.id)
    assert _TOKEN_GOOD in observed

    async with db_session_factory() as session:
        run = await get_job_run(session, run_id=run_id)
    assert run is not None
    assert run.status == JobRunStatus.FAILED, (
        "a run that lost most of its reads must not report completed"
    )
    assert run.retry_count == 1
    assert run.error is not None and "8/10" in run.error
    assert run.checkpoint == {"attempted": 10, "failed": 8}


@pytest.mark.integration
async def test_failed_reads_are_retried_with_backoff_before_being_counted(
    db_session_factory: async_sessionmaker[AsyncSession],
    scripted_rpc_reader: _ScriptedRpcReader,
    test_secret_key: str,
) -> None:
    """AUD-463: the -32603 burst recovers across runs under load (300 -> 22 ->
    14), the signature of provider throttling rather than a dead token
    contract — a token that fails once and then succeeds must be retried
    within the same run instead of being written off for the hour."""
    async with db_session_factory() as session:
        wallet = await add_wallet(session, address=_WALLET_A, label="A")
        await session.commit()

    await _add_monitored_pair(
        db_session_factory, wallet_id=wallet.id, token_address=_TOKEN_RECOVERS
    )
    scripted_rpc_reader.scripts[_TOKEN_RECOVERS] = [RuntimeError("-32603: Internal error"), None]

    async with db_session_factory() as session:
        run_id = await enqueue_job(session, kind=JobKind.BALANCE_SCAN)
        await session.commit()

    await _run_balance_scan(db_session_factory, run_id=run_id)

    observed = await _observed_tokens(db_session_factory, wallet_id=wallet.id)
    assert _TOKEN_RECOVERS in observed, "a read that succeeds on retry must still be recorded"

    async with db_session_factory() as session:
        run = await get_job_run(session, run_id=run_id)
    assert run is not None
    assert run.status == JobRunStatus.COMPLETED
    assert run.checkpoint == {"attempted": 2, "failed": 0}, (
        "a failure that recovered on retry must not be counted against the run"
    )
