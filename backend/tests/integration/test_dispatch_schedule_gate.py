"""Regression tests for the schedule gate inside ``claim_job`` (AUD P0 scheduling).

The original defect: dispatch decided due-ness only from lease + retry budget, so
a job re-ran on every 5s worker poll and the interval configured in the US4
SchedulesPage had no effect. ``claim_job`` now consults the ``schedule`` row
(``enabled``, ``paused_at``, ``next_run_at``, ``freshness_s``); these tests pin
that behaviour so the dispatch/schedule link cannot silently rot again.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from audr.jobs.store import JobKind, claim_job, complete_job

pytestmark = pytest.mark.integration


async def _insert_schedule(
    session: AsyncSession,
    *,
    kind: JobKind,
    enabled: bool = True,
    paused_at: datetime | None = None,
    next_run_at: datetime | None = None,
    freshness_s: int | None = None,
    last_run_at: datetime | None = None,
) -> None:
    await session.execute(
        sa.text(
            """
            INSERT INTO schedule
              (id, kind, enabled, paused_at, next_run_at, freshness_s, last_run_at)
            VALUES
              (:id, :kind, :enabled, :paused_at, :next_run_at, :freshness_s, :last_run_at)
            ON CONFLICT (kind) DO UPDATE SET
              enabled      = EXCLUDED.enabled,
              paused_at    = EXCLUDED.paused_at,
              next_run_at  = EXCLUDED.next_run_at,
              freshness_s  = EXCLUDED.freshness_s,
              last_run_at  = EXCLUDED.last_run_at
            """
        ),
        {
            "id": str(uuid.uuid4()),
            "kind": kind.value,
            "enabled": enabled,
            "paused_at": paused_at,
            "next_run_at": next_run_at,
            "freshness_s": freshness_s,
            "last_run_at": last_run_at,
        },
    )
    await session.flush()


async def test_claim_blocked_until_next_run_at(db_session: AsyncSession) -> None:
    """A future ``next_run_at`` makes the job not due — no run is created."""
    now = datetime.now(tz=UTC)
    await _insert_schedule(
        db_session, kind=JobKind.BALANCE_SCAN, next_run_at=now + timedelta(hours=1)
    )

    assert await claim_job(db_session, kind=JobKind.BALANCE_SCAN) is None


async def test_claim_allowed_once_next_run_at_has_passed(db_session: AsyncSession) -> None:
    """A past ``next_run_at`` lets the job run."""
    now = datetime.now(tz=UTC)
    await _insert_schedule(
        db_session, kind=JobKind.BALANCE_SCAN, next_run_at=now - timedelta(minutes=1)
    )

    assert await claim_job(db_session, kind=JobKind.BALANCE_SCAN) is not None


async def test_job_does_not_rerun_before_interval_elapses(db_session: AsyncSession) -> None:
    """The configured interval (``freshness_s``) survives a completed run.

    This is the poll-loop regression: after a run completes, the next poll must
    not immediately re-claim the same kind while its cooldown window is open.
    """
    await _insert_schedule(db_session, kind=JobKind.VALUATION, freshness_s=3600)

    run_id = await claim_job(db_session, kind=JobKind.VALUATION)
    assert run_id is not None
    await complete_job(db_session, run_id=run_id)
    await db_session.flush()

    # Immediately afterwards the interval has not elapsed → not due.
    assert await claim_job(db_session, kind=JobKind.VALUATION) is None

    # Backdate the last run beyond the interval → due again.
    await db_session.execute(
        sa.text(
            "UPDATE schedule SET last_run_at = now() - interval '2 hours',"
            " next_run_at = NULL WHERE kind = :kind"
        ),
        {"kind": JobKind.VALUATION.value},
    )
    await db_session.flush()
    assert await claim_job(db_session, kind=JobKind.VALUATION) is not None


async def test_disabled_and_paused_schedules_block_dispatch(db_session: AsyncSession) -> None:
    """``enabled = false`` and a set ``paused_at`` both stop dispatch."""
    await _insert_schedule(db_session, kind=JobKind.DISCOVERY, enabled=False)
    assert await claim_job(db_session, kind=JobKind.DISCOVERY) is None

    await db_session.execute(
        sa.text("UPDATE schedule SET enabled = true, paused_at = now() WHERE kind = :kind"),
        {"kind": JobKind.DISCOVERY.value},
    )
    await db_session.flush()
    assert await claim_job(db_session, kind=JobKind.DISCOVERY) is None


async def test_missing_schedule_row_falls_back_to_a_conservative_interval(
    db_session: AsyncSession,
) -> None:
    """No schedule row configured must not mean "always due" (AUD-356).

    That was the old default, and it is what turned an empty ``schedule``
    table into a ~2-claims/sec hot loop in production: every poll re-claimed
    and immediately completed the job with zero cooldown. The first-ever run
    is still allowed immediately (there is nothing to be fresher than yet);
    subsequent polls fall back to a conservative in-code interval instead.
    """
    await db_session.execute(
        sa.text("DELETE FROM schedule WHERE kind = :kind"),
        {"kind": JobKind.NEWS_REFRESH.value},
    )
    await db_session.flush()

    run_id = await claim_job(db_session, kind=JobKind.NEWS_REFRESH)
    assert run_id is not None, "the first-ever run with no schedule row must still be allowed"
    await complete_job(db_session, run_id=run_id)
    await db_session.flush()

    # Immediately afterwards: no schedule row, but a very recent prior run —
    # must not hot-loop.
    assert await claim_job(db_session, kind=JobKind.NEWS_REFRESH) is None

    # Backdate the only prior run beyond the conservative default → due again.
    await db_session.execute(
        sa.text(
            "UPDATE job_run SET completed_at = now() - interval '1 hour'"
            " WHERE kind = :kind AND status = 'completed'"
        ),
        {"kind": JobKind.NEWS_REFRESH.value},
    )
    await db_session.flush()
    assert await claim_job(db_session, kind=JobKind.NEWS_REFRESH) is not None
