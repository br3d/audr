"""Regression tests for AUD-318 — four job lifecycle bugs.

1. trigger_job created zombie in_progress rows (no worker executed them).
2. worker_status table was never written; /status always returned "unknown".
3. CoinGeckoError in quote_refresh was silently recorded as success.
4. Coalesced POST /jobs fallback fabricated a random run_id.

These tests lock in the corrected lifecycle behaviour.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import httpx
import pytest
import respx
import sqlalchemy as sa
from httpx import Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audr.api.app import app
from audr.db import get_db
from audr.jobs.quotes import handle_quote_refresh
from audr.jobs.store import (
    JobKind,
    JobRunStatus,
    claim_job,
    enqueue_job,
    get_job_run,
    upsert_worker_status,
)
from audr.jobs.worker import Worker
from audr.operations.init_key import init_key
from audr.settings.quotes import save_coingecko_credentials

_BASE = "http://test"
_JOBS_URL = "/api/v1/jobs"
_STATUS_URL = "/api/v1/status"
_SETUP_URL = "/api/v1/setup"
_PASSWORD = "correct-horse-battery-staple-99"
_COINGECKO_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _clean_tables(
    db_session_factory: async_sessionmaker[AsyncSession],
    test_secret_key: str,
) -> None:
    async with db_session_factory() as session:
        async with session.begin():
            await session.execute(sa.text("DELETE FROM quote_observation"))
            await session.execute(sa.text("DELETE FROM quote_set"))
            await session.execute(sa.text("DELETE FROM job_run"))
            await session.execute(sa.text("DELETE FROM worker_status"))
            await session.execute(sa.text("DELETE FROM integration"))
            await session.execute(sa.text("DELETE FROM login_attempt"))
            await session.execute(sa.text("DELETE FROM session"))
            await session.execute(sa.text("DELETE FROM owner"))
            await session.execute(sa.text("DELETE FROM key_state"))
    async with db_session_factory() as session:
        async with session.begin():
            await init_key(session)


def _make_override(factory: async_sessionmaker[AsyncSession]):
    async def _override() -> AsyncGenerator[AsyncSession]:
        async with factory() as session:
            yield session

    return _override


@pytest.fixture()
async def http_client(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[httpx.AsyncClient]:
    app.dependency_overrides[get_db] = _make_override(db_session_factory)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=_BASE) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


async def _setup_and_get_csrf(client: httpx.AsyncClient) -> str:
    r = await client.post(_SETUP_URL, json={"password": _PASSWORD})
    assert r.status_code == 201
    return r.json()["csrf_token"]


# ---------------------------------------------------------------------------
# Bug 1 — POST /jobs must enqueue a pending run, not a zombie in_progress
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_trigger_job_creates_pending_not_in_progress(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """POST /jobs enqueues a *pending* row that a worker can claim later."""
    csrf = await _setup_and_get_csrf(http_client)
    r = await http_client.post(
        _JOBS_URL, json={"kind": "balances"}, headers={"x-csrf-token": csrf}
    )
    assert r.status_code == 200
    data = r.json()
    run_id = uuid.UUID(data["run_id"])
    assert data["coalesced"] is False

    async with db_session_factory() as session:
        run = await get_job_run(session, run_id=run_id)
    assert run is not None
    assert run.status == JobRunStatus.PENDING, (
        f"Expected pending, got {run.status!r} — POST /jobs must enqueue, not claim"
    )


@pytest.mark.integration
async def test_worker_picks_up_enqueued_pending_run(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A non-on-demand Worker drains pending rows before the scheduled claim."""
    async with db_session_factory() as session:
        run_id = await enqueue_job(session, kind=JobKind.BALANCE_SCAN)
        await session.commit()

    executed: list[uuid.UUID] = []

    async def _handler(session: AsyncSession, rid: uuid.UUID) -> None:
        executed.append(rid)

    worker = Worker(db_session_factory, kind=JobKind.BALANCE_SCAN, handler=_handler)
    did_work = await worker.run_once()

    assert did_work is True
    assert executed == [run_id], "Worker must have executed the enqueued run"

    async with db_session_factory() as session:
        run = await get_job_run(session, run_id=run_id)
    assert run is not None
    assert run.status == JobRunStatus.COMPLETED


# ---------------------------------------------------------------------------
# Bug 4 — coalesced response must return a real run_id (never a fabricated one)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_trigger_job_coalesces_in_progress_returns_real_id(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When a run is already in_progress, POST /jobs returns its real run_id."""
    async with db_session_factory() as session:
        existing_id = await claim_job(session, kind=JobKind.DISCOVERY, max_retries=3)
        await session.commit()
    assert existing_id is not None

    csrf = await _setup_and_get_csrf(http_client)
    r = await http_client.post(
        _JOBS_URL, json={"kind": "discovery"}, headers={"x-csrf-token": csrf}
    )
    assert r.status_code == 200
    data = r.json()
    assert data["coalesced"] is True
    assert uuid.UUID(data["run_id"]) == existing_id, (
        "Coalesced response must return the existing run's id, not a fabricated one"
    )


@pytest.mark.integration
async def test_trigger_job_coalesces_pending_returns_real_id(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When a run is already pending, POST /jobs returns its real run_id."""
    async with db_session_factory() as session:
        existing_id = await enqueue_job(session, kind=JobKind.DISCOVERY)
        await session.commit()

    csrf = await _setup_and_get_csrf(http_client)
    r = await http_client.post(
        _JOBS_URL, json={"kind": "discovery"}, headers={"x-csrf-token": csrf}
    )
    assert r.status_code == 200
    data = r.json()
    assert data["coalesced"] is True
    assert uuid.UUID(data["run_id"]) == existing_id, (
        "Coalesced response must return the pending run's id, not a fabricated one"
    )


# ---------------------------------------------------------------------------
# Bug 2 — worker_status heartbeat must be written
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_upsert_worker_status_creates_row(db_session: AsyncSession) -> None:
    """upsert_worker_status inserts a new row on first call."""
    wid = "test-worker-" + str(uuid.uuid4())
    await upsert_worker_status(db_session, worker_id=wid, status="idle")

    row = (
        await db_session.execute(
            sa.text(
                "SELECT status FROM worker_status WHERE worker_id = :wid"
            ),
            {"wid": wid},
        )
    ).first()
    assert row is not None
    assert row[0] == "idle"


@pytest.mark.integration
async def test_upsert_worker_status_updates_on_conflict(db_session: AsyncSession) -> None:
    """Subsequent calls update the existing row (no duplicates)."""
    wid = "test-worker-" + str(uuid.uuid4())
    await upsert_worker_status(db_session, worker_id=wid, status="idle")
    await upsert_worker_status(db_session, worker_id=wid, status="running")

    rows = (
        await db_session.execute(
            sa.text("SELECT status FROM worker_status WHERE worker_id = :wid"),
            {"wid": wid},
        )
    ).fetchall()
    assert len(rows) == 1, "ON CONFLICT must update, not insert a second row"
    assert rows[0][0] == "running"


@pytest.mark.integration
async def test_status_endpoint_reflects_worker_heartbeat(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """/status shows running worker after a heartbeat row is upserted."""
    wid = "proc-" + str(uuid.uuid4())
    async with db_session_factory() as session:
        await upsert_worker_status(session, worker_id=wid, status="idle")
        await session.commit()

    await _setup_and_get_csrf(http_client)
    r = await http_client.get(_STATUS_URL)
    assert r.status_code == 200
    data = r.json()
    assert data["worker"]["status"] == "running", (
        "Worker with a fresh heartbeat must appear as 'running' in /status"
    )
    assert data["worker"]["last_heartbeat_at"] is not None


# ---------------------------------------------------------------------------
# Bug 3 — CoinGeckoError must produce a failed run, not a succeeded one
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_quote_refresh_provider_error_marks_run_failed(
    db_session_factory: async_sessionmaker[AsyncSession],
    test_secret_key: str,
) -> None:
    """handle_quote_refresh re-raises CoinGeckoError so the worker records failure."""
    async with db_session_factory() as session:
        # Configure the CoinGecko integration so the handler doesn't return early.
        await save_coingecko_credentials(session, api_key="test-key-aud318")

        # Insert a minimal asset + balance observation so the handler fetches prices.
        asset_id = uuid.uuid4()
        await session.execute(
            sa.text(
                "INSERT INTO asset (id, token_address, symbol, decimals)"
                " VALUES (:id, :addr, 'TST', 18)"
            ),
            {"id": str(asset_id), "addr": "0x" + "ab" * 20},
        )
        wallet_id = uuid.uuid4()
        await session.execute(
            sa.text(
                "INSERT INTO wallet (id, address, label, status)"
                " VALUES (:id, :addr, 'w', 'active')"
            ),
            {"id": str(wallet_id), "addr": "0x" + "11" * 20},
        )
        await session.execute(
            sa.text(
                "INSERT INTO balance_observation"
                " (id, wallet_id, asset_id, raw_amount, block_number)"
                " VALUES (:id, :wid, :aid, 1000000, 100)"
            ),
            {"id": str(uuid.uuid4()), "wid": str(wallet_id), "aid": str(asset_id)},
        )

        # Enqueue the run (pending); the worker will claim it via claim_pending_job.
        run_id = await enqueue_job(session, kind=JobKind.QUOTE_REFRESH)
        await session.commit()

    with respx.mock(assert_all_called=False):
        # CoinGecko returns 429 → RateLimitError (subclass of CoinGeckoError)
        respx.get(_COINGECKO_PRICE_URL).mock(return_value=Response(429))

        worker = Worker(
            db_session_factory,
            kind=JobKind.QUOTE_REFRESH,
            handler=handle_quote_refresh,
        )
        # Worker must return True (it processed the run) and record failure.
        did_work = await worker.run_once()

    assert did_work is True

    async with db_session_factory() as session:
        run = await get_job_run(session, run_id=run_id)
    assert run is not None
    assert run.status == JobRunStatus.FAILED, (
        f"Expected failed, got {run.status!r} — CoinGeckoError must not be swallowed"
    )
