"""Regression tests for AUD-356: transient failures must not brick a job kind.

Before this fix, ``claim_job`` counted every ``failed`` run since the last
``completed`` run with no time decay and no backoff — once the count reached
``max_retries`` the kind was blocked *forever*, with no timeout, no backoff,
and no self-reset. In production this is exactly what happened: the RPC
returned HTTP 429 three times in a row (itself caused by the schedule
hot-loop — see ``test_dispatch_schedule_gate.py``), and ``balance_scan``
silently stopped dispatching for two days until someone manually flipped the
failed rows to ``cancelled`` in the database.

These tests pin the fix: a failure streak now delays the next claim with
exponential backoff (capped), so the kind always eventually retries on its
own once enough time has passed — no manual DB edit required.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from audr.jobs.store import JobKind, claim_job

pytestmark = pytest.mark.integration


async def _insert_failed_run(session: AsyncSession, *, kind: JobKind, age: str) -> None:
    """Insert a ``failed`` job_run row for *kind*, backdated by *age* (e.g. '2 hours')."""
    await session.execute(
        sa.text(
            f"""
            INSERT INTO job_run (id, kind, status, created_at, completed_at, retry_count)
            VALUES (gen_random_uuid(), :kind, 'failed',
                    now() - interval '{age}', now() - interval '{age}', 1)
            """  # noqa: S608 — age is a test-local literal, not user input
        ),
        {"kind": kind.value},
    )
    await session.flush()


async def _backdate_failed_runs(
    session: AsyncSession, *, kind: JobKind, interval: str
) -> None:
    await session.execute(
        sa.text(
            f"""
            UPDATE job_run
            SET created_at = now() - interval '{interval}',
                completed_at = now() - interval '{interval}'
            WHERE kind = :kind AND status = 'failed'
            """  # noqa: S608 — interval is a test-local literal, not user input
        ),
        {"kind": kind.value},
    )
    await session.flush()


async def test_three_consecutive_failures_self_heal_after_backoff(
    db_session: AsyncSession,
) -> None:
    """3 consecutive failures back off, then retry on their own once the
    backoff window has elapsed — no manual DB intervention (AUD-356)."""
    max_retries = 3
    for _ in range(max_retries):
        await _insert_failed_run(db_session, kind=JobKind.BALANCE_SCAN, age="0 seconds")

    # Immediately after the 3rd failure: still inside the backoff window.
    assert (
        await claim_job(db_session, kind=JobKind.BALANCE_SCAN, max_retries=max_retries)
        is None
    )

    # Once the backoff window has clearly elapsed, the kind claims again on
    # its own — this is the behaviour that was missing before AUD-356.
    await _backdate_failed_runs(db_session, kind=JobKind.BALANCE_SCAN, interval="2 hours")

    run_id = await claim_job(db_session, kind=JobKind.BALANCE_SCAN, max_retries=max_retries)
    assert run_id is not None, (
        "3 transient failures must self-heal after backoff — a job kind "
        "must never require manual DB intervention to recover (AUD-356)"
    )


async def test_backoff_grows_then_caps_instead_of_blocking_forever(
    db_session: AsyncSession,
) -> None:
    """A streak far longer than max_retries still eventually retries: the
    backoff grows (hammering a broken provider less) but never becomes the
    old permanent block."""
    max_retries = 3
    for _ in range(max_retries + 5):  # well beyond the old hard cutoff
        await _insert_failed_run(db_session, kind=JobKind.DISCOVERY, age="0 seconds")

    assert (
        await claim_job(db_session, kind=JobKind.DISCOVERY, max_retries=max_retries) is None
    )

    # The backoff cap (with max_retries=3) tops out well under an hour —
    # comfortably past it regardless of how long the streak grew.
    await _backdate_failed_runs(db_session, kind=JobKind.DISCOVERY, interval="1 hour")

    assert (
        await claim_job(db_session, kind=JobKind.DISCOVERY, max_retries=max_retries)
        is not None
    )


async def test_old_failure_streak_does_not_gate_dispatch_today(
    db_session: AsyncSession,
) -> None:
    """A failure streak from a day-old incident must not still be blocking
    dispatch today — the streak decays with time, it isn't a permanent mark."""
    max_retries = 3
    for _ in range(max_retries):
        await _insert_failed_run(db_session, kind=JobKind.EVENT_INDEXER, age="48 hours")

    assert (
        await claim_job(db_session, kind=JobKind.EVENT_INDEXER, max_retries=max_retries)
        is not None
    )
