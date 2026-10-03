"""Regression tests for on-demand integration validation jobs (AUD-313).

Before the fix, POST /validate created an ``in_progress`` job that no worker
handled, so the health badge was stuck on "Validating…" forever.  These tests
lock in the queue → claim → execute → complete/fail lifecycle.
"""

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audr.jobs.store import (
    JobKind,
    JobRunStatus,
    claim_pending_job,
    enqueue_job,
    get_job_run,
)
from audr.jobs.worker import Worker


async def _delete_run(factory: async_sessionmaker[AsyncSession], run_id: uuid.UUID) -> None:
    async with factory() as session:
        await session.execute(sa.text("DELETE FROM job_run WHERE id = :id"), {"id": run_id})
        await session.commit()


@pytest.mark.integration
async def test_enqueue_creates_pending_run(db_session: AsyncSession) -> None:
    run_id = await enqueue_job(db_session, kind=JobKind.VALIDATE_RPC)
    run = await get_job_run(db_session, run_id=run_id)
    assert run is not None
    assert run.status == JobRunStatus.PENDING


@pytest.mark.integration
async def test_claim_pending_promotes_to_in_progress(db_session: AsyncSession) -> None:
    run_id = await enqueue_job(db_session, kind=JobKind.VALIDATE_RPC)

    claimed = await claim_pending_job(db_session, kind=JobKind.VALIDATE_RPC)
    assert claimed == run_id
    run = await get_job_run(db_session, run_id=run_id)
    assert run is not None
    assert run.status == JobRunStatus.IN_PROGRESS

    # Nothing pending remains, and an active run blocks a second claim.
    assert await claim_pending_job(db_session, kind=JobKind.VALIDATE_RPC) is None


@pytest.mark.integration
async def test_claim_pending_returns_none_when_empty(db_session: AsyncSession) -> None:
    assert await claim_pending_job(db_session, kind=JobKind.VALIDATE_QUOTES) is None


@pytest.mark.integration
async def test_worker_completes_successful_validation(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """On-demand worker claims a queued run and marks it completed on success."""
    async with db_session_factory() as session:
        run_id = await enqueue_job(session, kind=JobKind.VALIDATE_RPC)
        await session.commit()

    try:

        async def ok_handler(session: AsyncSession, _run_id: object) -> None:
            return None

        worker = Worker(
            db_session_factory,
            kind=JobKind.VALIDATE_RPC,
            handler=ok_handler,
            on_demand=True,
        )
        did_work = await worker.run_once()
        assert did_work is True

        async with db_session_factory() as session:
            run = await get_job_run(session, run_id=run_id)
        assert run is not None
        assert run.status == JobRunStatus.COMPLETED
    finally:
        await _delete_run(db_session_factory, run_id)


@pytest.mark.integration
async def test_worker_fails_validation_with_error(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A raising handler records a failed run with the error message for the UI."""
    async with db_session_factory() as session:
        run_id = await enqueue_job(session, kind=JobKind.VALIDATE_QUOTES)
        await session.commit()

    try:

        async def bad_handler(session: AsyncSession, _run_id: object) -> None:
            raise ValueError("No CoinGecko API key configured.")

        worker = Worker(
            db_session_factory,
            kind=JobKind.VALIDATE_QUOTES,
            handler=bad_handler,
            on_demand=True,
        )
        await worker.run_once()

        async with db_session_factory() as session:
            run = await get_job_run(session, run_id=run_id)
        assert run is not None
        assert run.status == JobRunStatus.FAILED
        assert run.error == "No CoinGecko API key configured."
    finally:
        await _delete_run(db_session_factory, run_id)
