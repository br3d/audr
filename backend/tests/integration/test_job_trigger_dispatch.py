"""POST /jobs must not create zombie runs or fabricate run_ids (AUD-318).

Before this fix, POST /api/v1/jobs called claim_job directly from the API
process: it inserted an `in_progress` job_run row that no worker would ever
execute (workers only claim `in_progress` leases they themselves created via
heartbeat/completion bookkeeping — an externally-inserted `in_progress` row
just sits until the 5-minute stale-lease expiry, then a *different*,
freshly-claimed run starts in its place). The frontend's "Run now" button
therefore silently did nothing, and the coalesced-trigger fallback returned a
fabricated uuid4() for a run that did not exist, so the poll for that id
404'd.

The fix enqueues a `pending` row (the AUD-313 validation-job pattern) and
lets the worker claim and dispatch it via claim_pending_job. These tests lock
in: a real, worker-visible row is created; the returned run_id always exists;
and duplicate triggers coalesce onto that same row instead of stacking.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audr.api.app import app
from audr.db import get_db

_BASE = "http://test"
_V1 = "/api/v1"
_PASSWORD = "correct-horse-battery-staple-99"


def _make_override(factory: async_sessionmaker[AsyncSession]):
    async def _override() -> AsyncGenerator[AsyncSession]:
        async with factory() as session:
            yield session

    return _override


_CLEAN_ORDER = ("job_run", "login_attempt", "session", "owner")


async def _wipe(db_session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with db_session_factory() as session:
        async with session.begin():
            for tbl in _CLEAN_ORDER:
                await session.execute(text(f"DELETE FROM {tbl}"))  # noqa: S608 — tbl comes from the hardcoded _CLEAN_ORDER tuple above, not user input


@pytest.fixture(autouse=True)
async def _clean_tables(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[None]:
    await _wipe(db_session_factory)
    yield
    await _wipe(db_session_factory)


@pytest.fixture()
async def auth_client(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[tuple[httpx.AsyncClient, str]]:
    """ASGI client with owner initialised; yields (client, csrf_token)."""
    app.dependency_overrides[get_db] = _make_override(db_session_factory)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=_BASE) as c:
            r = await c.post(f"{_V1}/setup", json={"password": _PASSWORD})
            assert r.status_code == 201, f"setup failed: {r.text}"
            csrf = r.json()["csrf_token"]
            yield c, csrf
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.integration
async def test_trigger_job_enqueues_pending_not_in_progress(
    auth_client: tuple[httpx.AsyncClient, str],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """POST /jobs must create a `pending` row for the worker to claim.

    An `in_progress` row inserted directly by the API is the AUD-318 zombie
    bug: no worker's claim_job would ever pick it up, since claim_job only
    checks for an *existing* in_progress lease, it never resumes one.
    """
    c, csrf = auth_client
    r = await c.post(
        f"{_V1}/jobs",
        json={"kind": "discovery"},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 202
    run_id = r.json()["run_id"]
    assert r.json()["coalesced"] is False

    async with db_session_factory() as session:
        row = (
            await session.execute(text("SELECT status FROM job_run WHERE id = :id"), {"id": run_id})
        ).first()
    assert row is not None, "the returned run_id must reference a real row"
    assert row[0] == "pending", (
        f"expected a worker-claimable 'pending' row, got '{row[0]}' — an "
        "in_progress row inserted directly by the API is a zombie no worker "
        "will ever claim"
    )


@pytest.mark.integration
async def test_trigger_job_run_id_is_never_fabricated(
    auth_client: tuple[httpx.AsyncClient, str],
) -> None:
    """The run_id in the response must always be fetchable via GET /jobs/{id}.

    Before the fix, the coalesced fallback path returned uuid4() for a run
    that did not exist whenever claim_job returned None, so the frontend's
    poll against that id 404'd.
    """
    c, csrf = auth_client
    r = await c.post(
        f"{_V1}/jobs",
        json={"kind": "discovery"},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 202
    run_id = r.json()["run_id"]

    get_r = await c.get(f"{_V1}/jobs/{run_id}")
    assert get_r.status_code == 200, (
        f"run_id returned by POST /jobs must resolve via GET /jobs/{{id}}, got {get_r.status_code}"
    )


@pytest.mark.integration
async def test_trigger_job_coalesces_onto_existing_pending_run(
    auth_client: tuple[httpx.AsyncClient, str],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A second trigger while one is already queued must coalesce, not stack."""
    c, csrf = auth_client
    first = await c.post(
        f"{_V1}/jobs",
        json={"kind": "discovery"},
        headers={"x-csrf-token": csrf},
    )
    assert first.status_code == 202
    first_run_id = first.json()["run_id"]
    assert first.json()["coalesced"] is False

    second = await c.post(
        f"{_V1}/jobs",
        json={"kind": "discovery"},
        headers={"x-csrf-token": csrf},
    )
    assert second.status_code == 202
    assert second.json()["run_id"] == first_run_id
    assert second.json()["coalesced"] is True

    async with db_session_factory() as session:
        count = (
            await session.execute(
                text(
                    "SELECT COUNT(*) FROM job_run WHERE kind = 'discovery'"
                    " AND status IN ('pending', 'in_progress')"
                )
            )
        ).scalar()
    assert count == 1, "coalescing must not leave two live rows for the same kind"


@pytest.mark.integration
async def test_trigger_job_dispatches_via_worker_claim_pending(
    auth_client: tuple[httpx.AsyncClient, str],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """End-to-end: a triggered run is actually claimable and executable.

    This is the acceptance bar from AUD-318 — "Run now" must execute a real
    run visible in /jobs history, not just return a 200.
    """
    from audr.jobs.store import JobKind, JobRunStatus, get_job_run
    from audr.jobs.worker import Worker

    c, csrf = auth_client
    r = await c.post(
        f"{_V1}/jobs",
        json={"kind": "discovery"},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 202
    run_id = r.json()["run_id"]

    executed: list = []

    async def handler(_session: AsyncSession, claimed_run_id: object) -> None:
        executed.append(claimed_run_id)

    worker = Worker(db_session_factory, kind=JobKind.DISCOVERY, handler=handler)
    did_work = await worker.run_once()
    assert did_work is True
    assert str(executed[0]) == run_id

    async with db_session_factory() as session:
        run = await get_job_run(session, run_id=executed[0])
    assert run is not None
    assert run.status == JobRunStatus.COMPLETED
