"""Revision-checked schedule management with freshness, budget validation, and usage projections (T081 / US4)."""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class RevisionConflictError(Exception):
    """Raised when the expected_revision does not match the stored revision."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SELECT_SCHEDULE = """
SELECT
    id,
    kind,
    enabled,
    revision,
    paused_at,
    freshness_s,
    budget_calls_per_day,
    last_run_at,
    next_run_at,
    updated_at
FROM schedule
WHERE kind = :kind
"""


def _row_to_dict(row: tuple) -> dict:  # type: ignore[type-arg]
    return {
        "id": row[0],
        "kind": row[1],
        "enabled": row[2],
        "revision": row[3],
        "paused_at": row[4],
        "freshness_s": row[5],
        "budget_calls_per_day": row[6],
        "last_run_at": row[7],
        "next_run_at": row[8],
        "updated_at": row[9],
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def get_schedule(session: AsyncSession, *, kind: str) -> dict | None:  # type: ignore[type-arg]
    """Return the schedule row for *kind* as a dict, or None if not found."""
    result = await session.execute(sa.text(_SELECT_SCHEDULE), {"kind": kind})
    row = result.first()
    if row is None:
        return None
    return _row_to_dict(row)


async def update_schedule(
    session: AsyncSession,
    *,
    kind: str,
    freshness_s: int | None = None,
    budget_calls_per_day: int | None = None,
    expected_revision: int | None = None,
) -> dict:  # type: ignore[type-arg]
    """Update configurable fields on a schedule row.

    Raises:
        ValueError: if any supplied value fails validation.
        RevisionConflictError: if *expected_revision* is given and differs from
            the stored revision.
        LookupError: if no schedule row exists for *kind*.
    """
    if freshness_s is not None and freshness_s < 0:
        raise ValueError("freshness_s must be non-negative")
    if budget_calls_per_day is not None and budget_calls_per_day <= 0:
        raise ValueError("budget_calls_per_day must be positive")

    current = await get_schedule(session, kind=kind)
    if current is None:
        raise LookupError(f"no schedule row for kind={kind!r}")

    if expected_revision is not None and current["revision"] != expected_revision:
        raise RevisionConflictError(
            f"revision conflict for kind={kind!r}: "
            f"expected {expected_revision}, found {current['revision']}"
        )

    set_parts: list[str] = ["revision = revision + 1", "updated_at = now()"]
    params: dict = {"kind": kind}  # type: ignore[type-arg]

    if freshness_s is not None:
        set_parts.append("freshness_s = :freshness_s")
        params["freshness_s"] = freshness_s
    if budget_calls_per_day is not None:
        set_parts.append("budget_calls_per_day = :budget_calls_per_day")
        params["budget_calls_per_day"] = budget_calls_per_day

    await session.execute(
        sa.text(
            f"UPDATE schedule SET {', '.join(set_parts)} WHERE kind = :kind"  # noqa: S608
        ),
        params,
    )
    await session.flush()

    updated = await get_schedule(session, kind=kind)
    assert updated is not None
    return updated


async def pause_schedule(session: AsyncSession, *, kind: str) -> dict:  # type: ignore[type-arg]
    """Set paused_at = now() if the schedule is not already paused.

    Does NOT change the ``enabled`` flag.
    """
    current = await get_schedule(session, kind=kind)
    if current is None:
        raise LookupError(f"no schedule row for kind={kind!r}")

    if current["paused_at"] is None:
        await session.execute(
            sa.text("UPDATE schedule SET paused_at = now() WHERE kind = :kind"),
            {"kind": kind},
        )
        await session.flush()

    updated = await get_schedule(session, kind=kind)
    assert updated is not None
    return updated


async def resume_schedule(session: AsyncSession, *, kind: str) -> dict:  # type: ignore[type-arg]
    """Clear paused_at (set to NULL) so the schedule may fire again."""
    current = await get_schedule(session, kind=kind)
    if current is None:
        raise LookupError(f"no schedule row for kind={kind!r}")

    await session.execute(
        sa.text("UPDATE schedule SET paused_at = NULL WHERE kind = :kind"),
        {"kind": kind},
    )
    await session.flush()

    updated = await get_schedule(session, kind=kind)
    assert updated is not None
    return updated


async def is_due(session: AsyncSession, *, kind: str) -> bool:
    """Return True if the schedule should fire now.

    Conditions (all must hold):
    - ``enabled = true``
    - ``paused_at IS NULL``
    - ``freshness_s IS NULL``, OR ``last_run_at IS NULL``, OR
      ``now() - last_run_at >= freshness_s`` seconds.
    """
    row = await get_schedule(session, kind=kind)
    if row is None:
        return False
    if not row["enabled"]:
        return False
    if row["paused_at"] is not None:
        return False

    freshness_s: int | None = row["freshness_s"]
    if freshness_s is None:
        return True

    last_run_at: datetime | None = row["last_run_at"]
    if last_run_at is None:
        return True

    now = datetime.now(UTC)
    elapsed_s = int((now - last_run_at).total_seconds())
    return elapsed_s >= freshness_s


async def project_usage(
    session: AsyncSession, *, kind: str, days: int = 30
) -> dict:  # type: ignore[type-arg]
    """Project API call usage for *kind* over *days* days.

    Returns::

        {
            "kind": str,
            "days": int,
            "runs_per_day": int,
            "calls_per_run": int,
            "total_calls": int,
            "budget_calls_per_day": int | None,
            "over_budget": bool,
        }
    """
    row = await get_schedule(session, kind=kind)
    if row is None:
        raise LookupError(f"no schedule row for kind={kind!r}")

    freshness_s: int | None = row["freshness_s"]
    budget: int | None = row["budget_calls_per_day"]

    if freshness_s is None or freshness_s <= 0:
        runs_per_day = 0
    else:
        runs_per_day = max(1, 86400 // freshness_s)

    calls_per_run = 1
    total_calls = runs_per_day * calls_per_run * days

    over_budget: bool
    if budget is None:
        over_budget = False
    else:
        daily_calls = runs_per_day * calls_per_run
        over_budget = daily_calls > budget

    return {
        "kind": kind,
        "days": days,
        "runs_per_day": runs_per_day,
        "calls_per_run": calls_per_run,
        "total_calls": total_calls,
        "budget_calls_per_day": budget,
        "over_budget": over_budget,
    }
