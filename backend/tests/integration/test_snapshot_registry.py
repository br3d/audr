"""Integration tests for the Snapshot space registry DB access (AUD-513)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from audr.jobs.snapshot_registry import (
    list_due_spaces,
    record_space_failure,
    record_space_success,
)

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
async def _clean_snapshot_space(db_session: AsyncSession) -> None:
    await db_session.execute(text("DELETE FROM snapshot_space"))


async def _insert_space(
    db_session: AsyncSession,
    *,
    asset_symbol: str,
    space_id: str = "uniswap",
    enabled: bool = True,
    last_polled_at_sql: str = "NULL",
) -> uuid.UUID:
    row_id = uuid.uuid4()
    await db_session.execute(
        text(
            f"""
            INSERT INTO snapshot_space (id, asset_symbol, space_id, enabled, last_polled_at)
            VALUES (:id, :asset_symbol, :space_id, :enabled, {last_polled_at_sql})
            """  # noqa: S608 — last_polled_at_sql is a module-local constant, not user input
        ),
        {
            "id": str(row_id),
            "asset_symbol": asset_symbol,
            "space_id": space_id,
            "enabled": enabled,
        },
    )
    await db_session.flush()
    return row_id


async def test_list_due_spaces_includes_never_polled(db_session: AsyncSession) -> None:
    await _insert_space(db_session, asset_symbol="UNI")
    due = await list_due_spaces(db_session)
    assert [s.asset_symbol for s in due] == ["UNI"]


async def test_list_due_spaces_excludes_recently_polled(db_session: AsyncSession) -> None:
    await _insert_space(
        db_session, asset_symbol="UNI", last_polled_at_sql="now() - interval '1 minute'"
    )
    due = await list_due_spaces(db_session)
    assert due == []


async def test_list_due_spaces_includes_stale_poll(db_session: AsyncSession) -> None:
    await _insert_space(
        db_session, asset_symbol="UNI", last_polled_at_sql="now() - interval '1 day'"
    )
    due = await list_due_spaces(db_session)
    assert [s.asset_symbol for s in due] == ["UNI"]


async def test_list_due_spaces_excludes_disabled(db_session: AsyncSession) -> None:
    await _insert_space(db_session, asset_symbol="UNI", enabled=False)
    due = await list_due_spaces(db_session)
    assert due == []


async def test_record_space_success_resets_failures(db_session: AsyncSession) -> None:
    space_id = await _insert_space(db_session, asset_symbol="UNI")
    await record_space_failure(db_session, space_id=space_id, error="boom")
    await record_space_success(db_session, space_id=space_id)

    row = (
        await db_session.execute(
            text(
                "SELECT consecutive_failures, last_error, last_success_at"
                " FROM snapshot_space WHERE id = :id"
            ),
            {"id": str(space_id)},
        )
    ).one()
    assert row[0] == 0
    assert row[1] is None
    assert row[2] is not None


async def test_record_space_failure_increments_streak_without_touching_success(
    db_session: AsyncSession,
) -> None:
    space_id = await _insert_space(db_session, asset_symbol="UNI")
    await record_space_failure(db_session, space_id=space_id, error="HTTP 500")
    await record_space_failure(db_session, space_id=space_id, error="HTTP 500 again")

    row = (
        await db_session.execute(
            text(
                "SELECT consecutive_failures, last_error, last_success_at"
                " FROM snapshot_space WHERE id = :id"
            ),
            {"id": str(space_id)},
        )
    ).one()
    assert row[0] == 2
    assert row[1] == "HTTP 500 again"
    assert row[2] is None
