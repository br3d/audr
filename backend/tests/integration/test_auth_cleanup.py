"""Periodic cleanup of login_attempt / session rows (AUD-322).

Neither table was ever pruned before this, so both grew without bound.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audr.operations.cleanup import cleanup_expired_auth_rows

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
async def _clean_tables(db_session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Other test modules commit rows to these tables; start from a clean slate."""
    async with db_session_factory() as session:
        async with session.begin():
            await session.execute(sa.text("DELETE FROM login_attempt"))
            await session.execute(sa.text("DELETE FROM session"))


async def _insert_login_attempt(session: AsyncSession, *, attempted_at: datetime) -> uuid.UUID:
    attempt_id = uuid.uuid4()
    await session.execute(
        sa.text(
            "INSERT INTO login_attempt (id, attempted_at, success)"
            " VALUES (:id, :attempted_at, false)"
        ),
        {"id": str(attempt_id), "attempted_at": attempted_at},
    )
    return attempt_id


async def _insert_session(session: AsyncSession, *, expires_at: datetime) -> uuid.UUID:
    session_id = uuid.uuid4()
    await session.execute(
        sa.text(
            "INSERT INTO session (id, csrf_token, revoked, expires_at)"
            " VALUES (:id, 'x', false, :expires_at)"
        ),
        {"id": str(session_id), "expires_at": expires_at},
    )
    return session_id


async def test_cleanup_deletes_old_login_attempts_keeps_recent(
    db_session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    old_id = await _insert_login_attempt(db_session, attempted_at=now - timedelta(days=8))
    recent_id = await _insert_login_attempt(db_session, attempted_at=now - timedelta(hours=1))
    await db_session.flush()

    deleted = await cleanup_expired_auth_rows(db_session)
    assert deleted["login_attempt"] == 1

    remaining = await db_session.execute(sa.text("SELECT id FROM login_attempt"))
    remaining_ids = {row[0] for row in remaining}
    assert old_id not in remaining_ids
    assert recent_id in remaining_ids


async def test_cleanup_deletes_long_expired_sessions_keeps_live_ones(
    db_session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    long_expired_id = await _insert_session(db_session, expires_at=now - timedelta(days=8))
    live_id = await _insert_session(db_session, expires_at=now + timedelta(hours=1))
    await db_session.flush()

    deleted = await cleanup_expired_auth_rows(db_session)
    assert deleted["session"] == 1

    remaining = await db_session.execute(sa.text("SELECT id FROM session"))
    remaining_ids = {row[0] for row in remaining}
    assert long_expired_id not in remaining_ids
    assert live_id in remaining_ids


async def test_cleanup_is_a_noop_on_empty_tables(db_session: AsyncSession) -> None:
    deleted = await cleanup_expired_auth_rows(db_session)
    assert deleted == {"login_attempt": 0, "session": 0}
