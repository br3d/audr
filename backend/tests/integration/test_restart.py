"""Integration tests for system restart and recovery behavior.

Covers:
  - Key persistence: after init_key the wrapped key survives a simulated restart
    (a second call to get_master_key without calling init_key returns the same bytes).
  - Migration readiness: check_migration_readiness correctly reports whether the
    applied schema revision matches the current alembic head.
  - Key env errors: get_master_key raises appropriate errors when SECRET_KEY is absent
    or holds a different value than the one used to wrap the stored blob.
  - Worker lease recovery: a stale in_progress run is reclaimed once the lease window
    expires, simulating recovery after a worker crash.
  - Lease fencing: a second concurrent claim for the same job kind is rejected.

Requires (via fixtures in tests/conftest.py):
  - TEST_DATABASE_URL pointing at a test PostgreSQL instance.
  - test_secret_key fixture (sets SECRET_KEY to a safe test value).
  - key_state and job_run tables existing (alembic upgrade head).

Marker: integration
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from audr.jobs.store import JobKind, claim_job
from audr.operations.crypto import InvalidEnvelopeError, MissingKeyError
from audr.operations.init_key import get_master_key, init_key
from audr.operations.migrations import check_migration_readiness

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Autouse fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _clean_key_state(db_session: AsyncSession) -> None:
    """Remove any existing key_state rows so each test starts fresh."""
    await db_session.execute(text("DELETE FROM key_state"))
    await db_session.flush()
    yield


# ---------------------------------------------------------------------------
# Key persistence after simulated restart
# ---------------------------------------------------------------------------


async def test_persistent_volume_key_survives_restart(
    db_session: AsyncSession, test_secret_key: str
) -> None:
    """The master key stored by init_key is returned identically on a subsequent get.

    Simulates a restart: init_key writes the key once; get_master_key is then
    called directly (without init_key) to simulate a fresh process reading the
    persisted blob from the database volume.
    """
    await init_key(db_session)

    # Confirm the row was written to the DB.
    result = await db_session.execute(text("SELECT name FROM key_state WHERE name = 'master_key'"))
    assert result.first() is not None, "key_state row must exist after init_key"

    # Both calls must return the same 32-byte key (no init_key in-between).
    key_first = await get_master_key(db_session)
    key_second = await get_master_key(db_session)

    assert isinstance(key_first, bytes)
    assert len(key_first) == 32
    assert key_first == key_second


# ---------------------------------------------------------------------------
# Migration readiness
# ---------------------------------------------------------------------------


async def test_migration_failure_returns_readiness_false(
    db_session: AsyncSession,
) -> None:
    """check_migration_readiness returns up_to_date=False when DB is behind head.

    Simulates a database pinned at an old revision while the codebase has advanced
    to a newer one.  The head revision is patched so the test is environment-independent.
    """
    # Force the recorded schema revision to an old value (rolled back after the test).
    await db_session.execute(text("DELETE FROM alembic_version"))
    await db_session.execute(
        text("INSERT INTO alembic_version (version_num) VALUES ('0000_stale')")
    )
    await db_session.flush()

    with patch("audr.operations.migrations._get_head_revision", return_value="0001"):
        result = await check_migration_readiness(db_session)

    assert result["up_to_date"] is False
    assert result["current"] == "0000_stale"
    assert result["head"] == "0001"


async def test_migration_readiness_up_to_date(
    db_session: AsyncSession,
) -> None:
    """check_migration_readiness reports up_to_date=True when DB is at the current head.

    Assumes the test database has been fully migrated (alembic upgrade head) before
    the test suite runs.
    """
    result = await check_migration_readiness(db_session)

    assert result["up_to_date"] is True
    assert result["head"] is not None
    assert result["current"] == result["head"]


# ---------------------------------------------------------------------------
# Key environment errors
# ---------------------------------------------------------------------------


async def test_wrong_key_raises_missing_key_error(
    db_session: AsyncSession,
    test_secret_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_master_key raises when SECRET_KEY has been rotated to a different value."""
    await init_key(db_session)

    # Rotate to a different (still well-formed) 32-byte hex key.
    monkeypatch.setenv("SECRET_KEY", os.urandom(32).hex())

    with pytest.raises((InvalidEnvelopeError, MissingKeyError)):
        await get_master_key(db_session)


async def test_missing_key_env_raises_missing_key_error(
    db_session: AsyncSession,
    test_secret_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_master_key raises MissingKeyError when SECRET_KEY env var is absent."""
    await init_key(db_session)
    monkeypatch.delenv("SECRET_KEY", raising=False)

    with pytest.raises(MissingKeyError):
        await get_master_key(db_session)


# ---------------------------------------------------------------------------
# Worker lease recovery and fencing
# ---------------------------------------------------------------------------


async def test_worker_lease_recovered_after_crash(db_session: AsyncSession) -> None:
    """A stale in_progress lease is successfully reclaimed after the timeout window.

    The first claim represents a worker that has since crashed (heartbeat backdated
    beyond the lease timeout).  The second claim must succeed and return a new run_id.
    """
    crashed_run_id = await claim_job(db_session, kind=JobKind.VALUATION, max_retries=3)
    assert crashed_run_id is not None

    # Simulate a crashed worker by backdating the heartbeat past the lease timeout.
    await db_session.execute(
        text("UPDATE job_run SET heartbeat_at = now() - interval '10 minutes' WHERE id = :id"),
        {"id": crashed_run_id},
    )
    await db_session.flush()

    recovered_run_id = await claim_job(db_session, kind=JobKind.VALUATION, max_retries=3)
    assert recovered_run_id is not None
    assert recovered_run_id != crashed_run_id


async def test_multiple_worker_instances_do_not_double_claim(
    db_session: AsyncSession,
) -> None:
    """Two claims for the same active job kind: the second must return None.

    The partial unique index on (kind) WHERE status = 'in_progress' enforces
    that only one worker can hold the lease at a time.
    """
    run_id_1 = await claim_job(db_session, kind=JobKind.BALANCE_SCAN, max_retries=3)
    assert run_id_1 is not None

    run_id_2 = await claim_job(db_session, kind=JobKind.BALANCE_SCAN, max_retries=3)
    assert run_id_2 is None, "second claim for an active job kind must be fenced"
