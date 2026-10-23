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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
    """A run that stops heartbeating loses its lease — but is not immediately
    reclaimable (AUD-689): the expired lease is terminated as `failed`, which
    now feeds claim_job's own AUD-356 backoff streak instead of sitting in
    `pending` where claim_pending_job would bypass that gate. Once the
    backoff window elapses, the kind claims again on its own.
    """
    import sqlalchemy as sa

    run_id = await claim_job(db_session, kind=JobKind.BALANCE_SCAN, max_retries=3)
    assert run_id is not None

    # Simulate lease expiry by manually expiring the heartbeat in the DB.
    await db_session.execute(
        sa.text("UPDATE job_run SET heartbeat_at = now() - interval '10 minutes' WHERE id = :id"),
        {"id": run_id},
    )
    await db_session.flush()

    # Immediately after expiry: the lease is gone (the row is `failed`, not
    # reclaimable in place) but the backoff window is still open.
    assert await claim_job(db_session, kind=JobKind.BALANCE_SCAN, max_retries=3) is None
    run = await get_job_run(db_session, run_id=run_id)
    assert run is not None
    assert run.status == JobRunStatus.FAILED
    assert run.error is not None and run.error.startswith("lease expired:")

    # Once the backoff window has elapsed, a second worker can claim fresh.
    await db_session.execute(
        sa.text(
            "UPDATE job_run SET created_at = now() - interval '2 hours',"
            " completed_at = now() - interval '2 hours' WHERE id = :id"
        ),
        {"id": run_id},
    )
    await db_session.flush()

    run_id2 = await claim_job(db_session, kind=JobKind.BALANCE_SCAN, max_retries=3)
    assert run_id2 is not None
    assert run_id2 != run_id


@pytest.mark.integration
async def test_duplicate_claim_rejected(db_session: AsyncSession) -> None:
    """Two workers cannot hold an active lease on the same job kind."""
    run_id1 = await claim_job(db_session, kind=JobKind.VALUATION, max_retries=3)
    assert run_id1 is not None

    run_id2 = await claim_job(db_session, kind=JobKind.VALUATION, max_retries=3)
    assert run_id2 is None  # second claim rejected


@pytest.mark.integration
async def test_retry_budget_exhaustion_backs_off_immediately(db_session: AsyncSession) -> None:
    """A job that fails more than max_retries times is not immediately retryable.

    It is NOT blocked forever (see test_job_retry_backoff.py for AUD-356's
    self-healing backoff) — only immediately after the streak, while the
    exponential backoff window is still open.
    """
    max_retries = 2
    for _ in range(max_retries + 1):
        run_id = await claim_job(db_session, kind=JobKind.DISCOVERY, max_retries=max_retries)
        if run_id is None:
            break
        await fail_job(db_session, run_id=run_id, error="transient error")

    # Immediately afterwards: still inside the backoff window, not retryable yet.
    run_id = await claim_job(db_session, kind=JobKind.DISCOVERY, max_retries=max_retries)
    assert run_id is None


@pytest.mark.integration
async def test_restart_recovery_reclaims_orphaned_run(db_session: AsyncSession) -> None:
    """A job left in_progress by a crashed worker is terminated (not left
    reclaimable in place) on lease expiry, and a fresh run starts once
    claim_job's backoff window — now fed by that terminated row — elapses.
    """
    import sqlalchemy as sa

    # Simulate a crashed worker by inserting a stale in_progress run.
    run_id = await claim_job(db_session, kind=JobKind.VALUATION, max_retries=3)
    assert run_id is not None

    # Expire the lease.
    await db_session.execute(
        sa.text("UPDATE job_run SET heartbeat_at = now() - interval '10 minutes' WHERE id = :id"),
        {"id": run_id},
    )
    await db_session.flush()

    assert await claim_job(db_session, kind=JobKind.VALUATION, max_retries=3) is None

    await db_session.execute(
        sa.text(
            "UPDATE job_run SET created_at = now() - interval '2 hours',"
            " completed_at = now() - interval '2 hours' WHERE id = :id"
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
            "UPDATE job_run SET heartbeat_at = now() - interval '4 minutes' WHERE id = :id"
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
    run_id = await claim_job(db_session, kind=JobKind.VALUATION, max_retries=3)
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


# ---------------------------------------------------------------------------
# Lease-expiry resilience: dead_letter, backoff, on-demand retry (AUD-689)
# ---------------------------------------------------------------------------


async def _expire_lease(db_session: AsyncSession, *, run_id: object) -> None:
    import sqlalchemy as sa

    await db_session.execute(
        sa.text("UPDATE job_run SET heartbeat_at = now() - interval '10 minutes' WHERE id = :id"),
        {"id": run_id},
    )
    await db_session.flush()


async def _backdate(db_session: AsyncSession, *, run_id: object, interval: str) -> None:
    import sqlalchemy as sa

    await db_session.execute(
        sa.text(
            f"UPDATE job_run SET created_at = now() - interval '{interval}',"  # noqa: S608
            f" completed_at = now() - interval '{interval}' WHERE id = :id"
        ),
        {"id": run_id},
    )
    await db_session.flush()


@pytest.mark.integration
async def test_scheduled_lease_expiry_does_not_bypass_backoff_via_pending_queue(
    db_session: AsyncSession,
) -> None:
    """Regression for AUD-689 point 3: before the fix, an expired scheduled
    run was recycled straight to `pending`, where claim_pending_job (checked
    on every worker poll, unconditionally) reclaimed it instantly — skipping
    both the schedule freshness gate and the AUD-356 backoff streak. The
    expired run must not be visible to claim_pending_job at all.
    """
    from audr.jobs.store import claim_pending_job

    run_id = await claim_job(db_session, kind=JobKind.EVENT_INDEXER, max_retries=3)
    assert run_id is not None
    await _expire_lease(db_session, run_id=run_id)

    assert await claim_pending_job(db_session, kind=JobKind.EVENT_INDEXER) is None

    run = await get_job_run(db_session, run_id=run_id)
    assert run is not None
    assert run.status == JobRunStatus.FAILED


@pytest.mark.integration
async def test_poison_pill_lease_hits_dead_letter_after_max_retries(
    db_session: AsyncSession,
) -> None:
    """Regression for AUD-689 point 1: a job kind whose lease keeps expiring
    without ever completing (OOM/SIGKILL every attempt, so fail_job never
    runs) is bounded — the max_retries-th consecutive lease timeout lands in
    the terminal `dead_letter` state instead of being recycled forever.
    """
    max_retries = 3
    kind = JobKind.ASSET_ICON_REFRESH
    run_id = await claim_job(db_session, kind=kind, max_retries=max_retries)
    assert run_id is not None

    dead_lettered_id = None
    for _ in range(max_retries):
        await _expire_lease(db_session, run_id=run_id)
        # Triggers _expire_stale_leases' transition for run_id; whether this
        # same call also claims a fresh run depends on backoff — irrelevant
        # here, so just inspect run_id's resulting status afterwards.
        await claim_job(db_session, kind=kind, max_retries=max_retries)

        run = await get_job_run(db_session, run_id=run_id)
        assert run is not None
        if run.status == JobRunStatus.DEAD_LETTER:
            dead_lettered_id = run_id
            break
        assert run.status == JobRunStatus.FAILED

        # Clear claim_job's own backoff gate so a fresh attempt is claimable,
        # letting the next loop iteration drive another lease expiry.
        await _backdate(db_session, run_id=run_id, interval="2 hours")
        new_run_id = await claim_job(db_session, kind=kind, max_retries=max_retries)
        assert new_run_id is not None, "claim_job must keep retrying on its own (AUD-356)"
        run_id = new_run_id

    assert dead_lettered_id is not None, (
        "expected a lease that never completes to reach dead_letter within max_retries"
    )
    run = await get_job_run(db_session, run_id=dead_lettered_id)
    assert run is not None
    assert run.retry_count == 1


@pytest.mark.integration
async def test_on_demand_lease_expiry_requeues_under_budget(db_session: AsyncSession) -> None:
    """Regression for AUD-689 point 4: an interactive "Run now" request
    (enqueue_job) whose worker crashes mid-run (lease timeout, not a caught
    exception) is automatically requeued instead of the click being lost.
    """
    from audr.jobs.store import claim_pending_job, enqueue_job

    run_id = await enqueue_job(
        db_session, kind=JobKind.BALANCE_SCAN, max_retries=3, params={"wallet_id": "abc"}
    )
    claimed = await claim_pending_job(db_session, kind=JobKind.BALANCE_SCAN)
    assert claimed == run_id

    await _expire_lease(db_session, run_id=run_id)

    # This single call both (a) transitions run_id's expired lease to
    # failed-and-requeued, and (b) claims the freshly requeued pending row.
    retried_id = await claim_pending_job(db_session, kind=JobKind.BALANCE_SCAN)
    assert retried_id is not None
    assert retried_id != run_id

    original = await get_job_run(db_session, run_id=run_id)
    assert original is not None
    assert original.status == JobRunStatus.FAILED

    retried = await get_job_run(db_session, run_id=retried_id)
    assert retried is not None
    assert retried.retry_count == 1
    assert retried.params == {"wallet_id": "abc"}


@pytest.mark.integration
async def test_on_demand_exhausted_retries_stay_failed_no_further_requeue(
    db_session: AsyncSession,
) -> None:
    """An on-demand request that keeps failing stops retrying once its own
    max_retries budget is spent — it must not requeue forever."""
    from audr.jobs.store import claim_pending_job, enqueue_job, fail_job

    max_retries = 2
    await enqueue_job(db_session, kind=JobKind.VALIDATE_RPC, max_retries=max_retries)
    for _ in range(max_retries + 1):
        claimed = await claim_pending_job(db_session, kind=JobKind.VALIDATE_RPC)
        if claimed is None:
            break
        await fail_job(db_session, run_id=claimed, error="connection refused")

    assert await claim_pending_job(db_session, kind=JobKind.VALIDATE_RPC) is None


# ---------------------------------------------------------------------------
# Worker liveness heartbeat (AUD-318)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_upsert_worker_status_inserts_then_updates_same_row(
    db_session: AsyncSession,
) -> None:
    """The worker heartbeat keeps one row per worker_id and advances it in place.

    Nothing wrote worker_status before AUD-318, so GET /api/v1/status reported
    the worker as "unknown" forever.
    """
    import sqlalchemy as sa

    from audr.jobs.store import upsert_worker_status

    worker_id = "test-worker-aud-318"
    await upsert_worker_status(db_session, worker_id=worker_id, status="idle")

    first = (
        await db_session.execute(
            sa.text("SELECT status, last_heartbeat_at FROM worker_status WHERE worker_id = :wid"),
            {"wid": worker_id},
        )
    ).one()
    assert first[0] == "idle"
    assert first[1] is not None

    run_id = await claim_job(db_session, kind=JobKind.BALANCE_SCAN, max_retries=3)
    await upsert_worker_status(
        db_session, worker_id=worker_id, status="running", current_job_run_id=run_id
    )

    rows = (
        await db_session.execute(
            sa.text("SELECT status, current_job_run_id FROM worker_status WHERE worker_id = :wid"),
            {"wid": worker_id},
        )
    ).all()
    assert len(rows) == 1, "a restart or poll must not accumulate rows"
    assert rows[0][0] == "running"
    assert rows[0][1] == run_id


# ---------------------------------------------------------------------------
# On-demand trigger dispatch for schedule-driven kinds (AUD-318)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_worker_serves_enqueued_pending_run_before_schedule_claim(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A "Run now" request (enqueue_job) must be dispatched by the very next poll.

    Before AUD-318, POST /jobs called claim_job directly from the API process,
    inserting an in_progress row no worker would ever claim — it sat as a
    zombie until the 5-minute stale-lease expiry, then a fresh schedule-driven
    run started instead of the one the user asked for. The fix enqueues a
    pending row and relies on the worker to claim and run it: Worker.run_once
    must check the pending queue even for a kind that also runs on its own
    schedule (DISCOVERY here), not only for on_demand=True workers.
    """
    from audr.jobs.store import JobRunStatus, enqueue_job, get_job_run
    from audr.jobs.worker import Worker

    async with db_session_factory() as session:
        run_id = await enqueue_job(session, kind=JobKind.DISCOVERY)
        await session.commit()

    executed_run_ids: list = []

    async def handler(_session: AsyncSession, run_id: object) -> None:
        executed_run_ids.append(run_id)

    try:
        worker = Worker(db_session_factory, kind=JobKind.DISCOVERY, handler=handler)
        did_work = await worker.run_once()
        assert did_work is True
        assert executed_run_ids == [run_id], (
            "the worker must dispatch the enqueued run on its very next poll, "
            "not leave it pending until claim_job's schedule-driven path fires"
        )

        async with db_session_factory() as session:
            run = await get_job_run(session, run_id=run_id)
        assert run is not None
        assert run.status == JobRunStatus.COMPLETED
    finally:
        async with db_session_factory() as session:
            import sqlalchemy as sa

            await session.execute(sa.text("DELETE FROM job_run WHERE id = :id"), {"id": run_id})
            await session.commit()
