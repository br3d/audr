"""Failing integration tests for worker lease logic (T016).

Tests are intentionally FAILING until T017 creates the job tables and T018
implements audr.jobs.store and audr.jobs.worker.

Covers:
  - Lease loss: a worker that misses its heartbeat deadline loses its lease.
  - Duplicate claim: two workers cannot hold the same job kind simultaneously.
  - Retry budget: a job that exhausts its retry allowance is moved to failed.
  - Restart recovery: a crashed worker's in-flight job is reclaimed on restart.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from audr.jobs.store import (
    JobKind,
    JobRunStatus,
    claim_job,
    complete_job,
    fail_job,
    get_job_run,
    heartbeat,
    release_lease,
)


@pytest.mark.integration
async def test_lease_loss_after_missed_heartbeat(db_session: AsyncSession) -> None:
    """A run that stops heartbeating is reclaimed after the lease window expires."""
    run_id = await claim_job(db_session, kind=JobKind.BALANCE_SCAN, max_retries=3)
    assert run_id is not None

    # Simulate lease expiry by manually expiring the heartbeat in the DB.
    await db_session.execute(
        # pragma: nocover — SQL is DB-level, intentionally raw
        __import__("sqlalchemy").text(
            "UPDATE job_run SET heartbeat_at = now() - interval '10 minutes'"
            " WHERE id = :id"
        ),
        {"id": run_id},
    )
    await db_session.flush()

    # A second worker should now be able to claim the same job kind.
    run_id2 = await claim_job(db_session, kind=JobKind.BALANCE_SCAN, max_retries=3)
    assert run_id2 is not None
    assert run_id2 != run_id


@pytest.mark.integration
async def test_duplicate_claim_rejected(db_session: AsyncSession) -> None:
    """Two workers cannot hold an active lease on the same job kind."""
    run_id1 = await claim_job(db_session, kind=JobKind.QUOTE_REFRESH, max_retries=3)
    assert run_id1 is not None

    run_id2 = await claim_job(db_session, kind=JobKind.QUOTE_REFRESH, max_retries=3)
    assert run_id2 is None  # second claim rejected


@pytest.mark.integration
async def test_retry_budget_exhaustion_moves_to_failed(db_session: AsyncSession) -> None:
    """A job that fails more than max_retries times is marked as failed permanently."""
    max_retries = 2
    for _ in range(max_retries + 1):
        run_id = await claim_job(
            db_session, kind=JobKind.DISCOVERY, max_retries=max_retries
        )
        if run_id is None:
            break
        await fail_job(db_session, run_id=run_id, error="transient error")

    # Final state should be failed (not retryable).
    run_id = await claim_job(db_session, kind=JobKind.DISCOVERY, max_retries=max_retries)
    assert run_id is None


@pytest.mark.integration
async def test_restart_recovery_reclaims_orphaned_run(db_session: AsyncSession) -> None:
    """A job left in_progress by a crashed worker can be reclaimed after lease expiry."""
    # Simulate a crashed worker by inserting a stale in_progress run.
    run_id = await claim_job(
        db_session, kind=JobKind.VALUATION, max_retries=3
    )
    assert run_id is not None

    # Expire the lease.
    await db_session.execute(
        __import__("sqlalchemy").text(
            "UPDATE job_run SET heartbeat_at = now() - interval '10 minutes'"
            " WHERE id = :id"
        ),
        {"id": run_id},
    )
    await db_session.flush()

    new_run_id = await claim_job(db_session, kind=JobKind.VALUATION, max_retries=3)
    assert new_run_id is not None
    assert new_run_id != run_id


@pytest.mark.integration
async def test_heartbeat_extends_lease(db_session: AsyncSession) -> None:
    """A heartbeat call must advance heartbeat_at, preventing lease expiry."""
    run_id = await claim_job(db_session, kind=JobKind.BALANCE_SCAN, max_retries=3)
    assert run_id is not None

    # Manually rewind the heartbeat then extend it.
    await db_session.execute(
        __import__("sqlalchemy").text(
            "UPDATE job_run SET heartbeat_at = now() - interval '4 minutes'"
            " WHERE id = :id"
        ),
        {"id": run_id},
    )
    await db_session.flush()

    await heartbeat(db_session, run_id=run_id)

    run = await get_job_run(db_session, run_id=run_id)
    assert run is not None
    assert run.status == JobRunStatus.IN_PROGRESS


@pytest.mark.integration
async def test_complete_job_sets_done_status(db_session: AsyncSession) -> None:
    """complete_job transitions the run to completed."""
    run_id = await claim_job(db_session, kind=JobKind.QUOTE_REFRESH, max_retries=3)
    assert run_id is not None
    await complete_job(db_session, run_id=run_id)
    run = await get_job_run(db_session, run_id=run_id)
    assert run is not None
    assert run.status == JobRunStatus.COMPLETED


@pytest.mark.integration
async def test_release_lease_allows_immediate_reclaim(db_session: AsyncSession) -> None:
    """release_lease makes the job available for immediate re-claiming."""
    run_id = await claim_job(db_session, kind=JobKind.DISCOVERY, max_retries=3)
    assert run_id is not None
    await release_lease(db_session, run_id=run_id)

    new_run_id = await claim_job(db_session, kind=JobKind.DISCOVERY, max_retries=3)
    assert new_run_id is not None
