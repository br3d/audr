"""Integration tests for schedule management (audr.settings.schedules).

Covers:
  - Input validation: freshness_s sign, budget_calls_per_day lower bound
  - Optimistic locking via expected_revision (RevisionConflictError on stale reads)
  - Pause / resume lifecycle — paused_at toggled, enabled flag remains unchanged
  - API-call usage projection: runs_per_day, calls_per_run, total_calls
  - Budget enforcement: over_budget flag set when projected calls exceed daily cap
  - Freshness / cooldown: is_due() returns False inside cooldown window
  - Freshness / due: is_due() returns True once freshness_s has elapsed
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from audr.settings.schedules import (
    RevisionConflictError,
    is_due,
    pause_schedule,
    project_usage,
    resume_schedule,
    update_schedule,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _insert_schedule(
    session: AsyncSession,
    *,
    kind: str,
    enabled: bool = True,
    revision: int = 1,
    freshness_s: int | None = None,
    budget_calls_per_day: int | None = None,
    last_run_at: datetime | None = None,
) -> None:
    """Insert or overwrite a schedule row (post-migration-0002 schema, no cron_expr).

    Migration 0008 (AUD-356) now seeds a default row for balance_scan,
    discovery, quote_refresh, event_indexer, and news_refresh, so a plain
    INSERT for those kinds would hit ``uq_schedule_kind``. ON CONFLICT DO
    UPDATE keeps this helper working for both pre-seeded and ad-hoc kinds.
    """
    await session.execute(
        text(
            """
            INSERT INTO schedule
              (id, kind, enabled, revision,
               freshness_s, budget_calls_per_day, last_run_at)
            VALUES
              (:id, :kind, :enabled, :revision,
               :freshness_s, :budget, :last_run_at)
            ON CONFLICT (kind) DO UPDATE SET
              enabled              = EXCLUDED.enabled,
              revision             = EXCLUDED.revision,
              freshness_s          = EXCLUDED.freshness_s,
              budget_calls_per_day = EXCLUDED.budget_calls_per_day,
              last_run_at          = EXCLUDED.last_run_at,
              paused_at            = NULL
            """
        ),
        {
            "id": str(uuid.uuid4()),
            "kind": kind,
            "enabled": enabled,
            "revision": revision,
            "freshness_s": freshness_s,
            "budget": budget_calls_per_day,
            "last_run_at": last_run_at,
        },
    )
    await session.flush()


async def _fetch_schedule(session: AsyncSession, kind: str) -> tuple:
    """Return (enabled, paused_at, revision) for the named schedule."""
    row = (
        await session.execute(
            text("SELECT enabled, paused_at, revision FROM schedule WHERE kind = :kind"),
            {"kind": kind},
        )
    ).first()
    assert row is not None, f"No schedule row found for kind={kind!r}"
    return row  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


async def test_schedule_validation_rejects_negative_freshness(
    db_session: AsyncSession,
) -> None:
    """update_schedule raises ValueError when freshness_s is negative."""
    with pytest.raises(ValueError, match="freshness"):
        await update_schedule(
            db_session,
            kind="quote_refresh",
            freshness_s=-1,
        )


async def test_schedule_validation_rejects_zero_budget(
    db_session: AsyncSession,
) -> None:
    """update_schedule raises ValueError when budget_calls_per_day is <= 0."""
    with pytest.raises(ValueError, match="budget"):
        await update_schedule(
            db_session,
            kind="discovery",
            budget_calls_per_day=0,
        )


# ---------------------------------------------------------------------------
# Optimistic locking
# ---------------------------------------------------------------------------


async def test_schedule_revision_check(db_session: AsyncSession) -> None:
    """Updating a schedule with the wrong expected_revision raises RevisionConflictError."""
    await _insert_schedule(db_session, kind="valuation", revision=1)

    with pytest.raises(RevisionConflictError):
        await update_schedule(
            db_session,
            kind="valuation",
            freshness_s=3600,
            expected_revision=99,  # wrong — actual is 1
        )


# ---------------------------------------------------------------------------
# Pause / resume lifecycle
# ---------------------------------------------------------------------------


async def test_schedule_pause_and_resume(db_session: AsyncSession) -> None:
    """pause_schedule sets paused_at; resume_schedule clears it; enabled stays True."""
    await _insert_schedule(db_session, kind="balance_scan", enabled=True)

    await pause_schedule(db_session, kind="balance_scan")

    enabled, paused_at, _ = await _fetch_schedule(db_session, "balance_scan")
    assert paused_at is not None, "paused_at should be set after pause"
    assert enabled is True, "enabled must remain True after pause"

    await resume_schedule(db_session, kind="balance_scan")

    enabled2, paused_at2, _ = await _fetch_schedule(db_session, "balance_scan")
    assert paused_at2 is None, "paused_at should be cleared after resume"
    assert enabled2 is True, "enabled must remain True after resume"


# ---------------------------------------------------------------------------
# Usage projection
# ---------------------------------------------------------------------------


async def test_schedule_usage_projection_basic(db_session: AsyncSession) -> None:
    """project_usage returns a dict with non-negative int fields runs_per_day, calls_per_run,
    total_calls."""
    await _insert_schedule(
        db_session,
        kind="quote_refresh",
        freshness_s=3600,  # hourly = 24 runs/day
        budget_calls_per_day=200,
    )

    result = await project_usage(db_session, kind="quote_refresh", days=30)

    for key in ("runs_per_day", "calls_per_run", "total_calls"):
        assert key in result, f"Missing key {key!r} in project_usage result"
        assert isinstance(result[key], int), f"{key} should be int, got {type(result[key])}"
        assert result[key] >= 0, f"{key} should be non-negative"


async def test_schedule_usage_projection_respects_budget(
    db_session: AsyncSession,
) -> None:
    """project_usage includes over_budget=True when projected daily calls exceed the cap."""
    await _insert_schedule(
        db_session,
        kind="discovery",
        freshness_s=3600,  # 24 runs/day; calls_per_run defaults to >=1
        budget_calls_per_day=1,  # far below any realistic projection
    )

    result = await project_usage(db_session, kind="discovery", days=1)

    assert result.get("over_budget") is True, (
        "expected over_budget=True when budget_calls_per_day is less than projected calls"
    )


# ---------------------------------------------------------------------------
# Freshness / cooldown
# ---------------------------------------------------------------------------


async def test_schedule_cooldown_prevents_immediate_retrigger(
    db_session: AsyncSession,
) -> None:
    """is_due returns False when last_run_at is within the freshness_s window."""
    now = datetime.now(tz=UTC)
    last_run_at = now - timedelta(seconds=30)  # only 30 s ago

    await _insert_schedule(
        db_session,
        kind="balance_scan",
        freshness_s=300,  # 5-minute cooldown
        last_run_at=last_run_at,
    )

    assert await is_due(db_session, kind="balance_scan") is False


async def test_schedule_due_when_freshness_elapsed(db_session: AsyncSession) -> None:
    """is_due returns True when last_run_at is beyond the freshness_s window."""
    now = datetime.now(tz=UTC)
    last_run_at = now - timedelta(seconds=400)  # 400 s > freshness_s=300

    await _insert_schedule(
        db_session,
        kind="quote_refresh",
        freshness_s=300,
        last_run_at=last_run_at,
    )

    assert await is_due(db_session, kind="quote_refresh") is True
