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


# ---------------------------------------------------------------------------
# VALIDATE_LLM handler (AUD-743)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_validate_llm_raises_when_no_key_configured(
    db_session: AsyncSession, test_secret_key: str
) -> None:
    """No stored key is a validation *failure*, not a silent pass — the whole
    point of the button is to say so (AUD-743)."""
    from audr.jobs.validation import handle_validate_llm

    # No commit: the db_session fixture owns the surrounding transaction and
    # committing it closes the context manager out from under the test.
    await db_session.execute(sa.text("DELETE FROM integration WHERE kind = 'anthropic'"))

    with pytest.raises(ValueError, match="No Anthropic API key configured"):
        await handle_validate_llm(db_session, uuid.uuid4())


@pytest.mark.integration
async def test_validate_llm_probes_anthropic_with_the_stored_key(
    db_session: AsyncSession, test_secret_key: str
) -> None:
    """The handler decrypts the stored key and reaches Anthropic with it."""
    import respx
    from httpx import Response

    from audr.jobs.validation import handle_validate_llm
    from audr.settings.llm import save_anthropic_credentials

    await save_anthropic_credentials(db_session, api_key="sk-stored-test")

    with respx.mock(assert_all_called=True) as mock:
        route = mock.post("https://api.anthropic.com/v1/messages").mock(
            return_value=Response(200, json={"content": [], "usage": {}})
        )
        await handle_validate_llm(db_session, uuid.uuid4())

    assert route.calls[0].request.headers["x-api-key"] == "sk-stored-test"


@pytest.mark.integration
async def test_validate_llm_propagates_rejected_key(
    db_session: AsyncSession, test_secret_key: str
) -> None:
    """A key Anthropic rejects must surface as a failed run, not fail open the
    way the ingest gate does (AUD-658)."""
    import respx
    from httpx import Response

    from audr.jobs.validation import handle_validate_llm
    from audr.providers.llm import NoApiKey
    from audr.settings.llm import save_anthropic_credentials

    await save_anthropic_credentials(db_session, api_key="sk-revoked")

    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://api.anthropic.com/v1/messages").mock(return_value=Response(401))
        with pytest.raises(NoApiKey):
            await handle_validate_llm(db_session, uuid.uuid4())
