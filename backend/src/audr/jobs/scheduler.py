"""Persistent job scheduler with pause/resume, coalescing, and budget sharing (T082 / US4)."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audr.jobs.store import JobKind, claim_job
from audr.settings.schedules import coalesce_missed, is_due

logger = logging.getLogger(__name__)


class Scheduler:
    """Coalescing job scheduler with pause/resume and manual-trigger budgeting.

    Manual trigger requests for the same job kind are coalesced: if a run of
    that kind is already active or queued, no extra run is started.

    A separate per-kind manual-trigger budget (``manual_budget`` triggers per
    hour) prevents runaway interactive requests from hammering the API.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        manual_budget: int = 10,
    ) -> None:
        self._factory = session_factory
        self._manual_budget = manual_budget
        self._pending: dict[JobKind, asyncio.Event] = defaultdict(asyncio.Event)
        # Timestamps of recent manual triggers keyed by job kind.
        self._manual_triggers: dict[JobKind, list[datetime]] = defaultdict(list)

    # ------------------------------------------------------------------
    # Manual trigger
    # ------------------------------------------------------------------

    async def trigger(self, kind: JobKind, *, max_retries: int = 3) -> bool:
        """Request an immediate run of *kind*.

        Returns True if a new run was claimed, False if one was already active
        (coalesced), the retry budget is exhausted, or the manual-trigger
        budget for the past hour is exceeded.
        """
        if not self._within_manual_budget(kind):
            logger.warning(
                "trigger rejected: manual budget exceeded kind=%s (limit=%d/hr)",
                kind.value,
                self._manual_budget,
            )
            return False

        async with self._factory() as session:
            run_id = await claim_job(session, kind=kind, max_retries=max_retries)
            if run_id is None:
                logger.info("trigger coalesced kind=%s (already active)", kind.value)
                return False
            await session.commit()

        self._manual_triggers[kind].append(datetime.now(UTC))
        logger.info("trigger claimed kind=%s run_id=%s", kind.value, run_id)
        return True

    def _within_manual_budget(self, kind: JobKind) -> bool:
        """Return True if fewer than ``_manual_budget`` triggers fired in the past hour."""
        cutoff = datetime.now(UTC) - timedelta(hours=1)
        recent = [ts for ts in self._manual_triggers[kind] if ts >= cutoff]
        # Prune stale entries in-place.
        self._manual_triggers[kind] = recent
        return len(recent) < self._manual_budget

    # ------------------------------------------------------------------
    # Cron-based scheduling (original methods, kept intact)
    # ------------------------------------------------------------------

    async def due_schedules(self, session: AsyncSession) -> list[JobKind]:
        """Return job kinds whose next_run_at is in the past and schedule is enabled."""
        now = datetime.now(tz=UTC)
        result = await session.execute(
            sa.text(
                """
                SELECT kind FROM schedule
                WHERE enabled = true
                  AND paused_at IS NULL
                  AND (next_run_at IS NULL OR next_run_at <= :now)
                """
            ),
            {"now": now},
        )
        return [JobKind(row[0]) for row in result.fetchall()]

    async def advance_schedule(self, session: AsyncSession, kind: JobKind) -> None:
        """Update last_run_at = now() after a successful run.

        next_run_at update is intentionally left to cron-aware code that knows
        the expression.  For now it clears next_run_at so the job is not
        immediately re-triggered.
        """
        await session.execute(
            sa.text(
                """
                UPDATE schedule
                SET last_run_at = now(), next_run_at = NULL
                WHERE kind = :kind
                """
            ),
            {"kind": kind.value},
        )

    # ------------------------------------------------------------------
    # Pause / resume
    # ------------------------------------------------------------------

    async def pause(self, session: AsyncSession, kind: JobKind) -> None:
        """Set paused_at = now() on the schedule row for *kind*."""
        await session.execute(
            sa.text(
                "UPDATE schedule SET paused_at = now() WHERE kind = :kind AND paused_at IS NULL"
            ),
            {"kind": kind.value},
        )
        logger.info("schedule paused kind=%s", kind.value)

    async def resume(self, session: AsyncSession, kind: JobKind) -> None:
        """Clear paused_at (set to NULL) on the schedule row for *kind*."""
        await session.execute(
            sa.text(
                "UPDATE schedule SET paused_at = NULL WHERE kind = :kind"
            ),
            {"kind": kind.value},
        )
        logger.info("schedule resumed kind=%s", kind.value)

    # ------------------------------------------------------------------
    # Coalescing missed runs
    # ------------------------------------------------------------------

    async def coalesce_due(
        self, session: AsyncSession, kind: JobKind, now: datetime
    ) -> bool:
        """Return True if at least one cron slot was missed since last_run_at.

        Delegates to :func:`~audr.settings.schedules.coalesce_missed` which
        coalesces all missed slots into at most one (no backfill).
        """
        missed = await coalesce_missed(session, kind=kind.value, now=now)
        return missed > 0

    # ------------------------------------------------------------------
    # Tick — evaluate all kinds and return those that need to run
    # ------------------------------------------------------------------

    async def tick(
        self, session: AsyncSession, now: datetime | None = None
    ) -> list[JobKind]:
        """Evaluate every ``JobKind`` and return those that should run now.

        For each kind the logic is:

        1. Call :func:`~audr.settings.schedules.is_due` (respects ``freshness_s``
           and ``paused_at``).
        2. If due, also call :func:`coalesce_due` to handle the one-missed-run
           -after-downtime case (returns 0 if nothing was missed, but we still
           include the kind when ``is_due`` returned True).
        3. Kinds for which neither check fires are skipped.

        Returns the list of :class:`~audr.jobs.store.JobKind` values that
        should be dispatched this tick.
        """
        if now is None:
            now = datetime.now(UTC)

        due: list[JobKind] = []

        for kind in JobKind:
            try:
                due_flag = await is_due(session, kind=kind.value)
            except Exception:
                logger.exception("tick: is_due failed kind=%s", kind.value)
                continue

            if due_flag:
                due.append(kind)
                logger.debug("tick: kind=%s is due", kind.value)
                continue

            # Even if freshness gate is not met, check whether we missed a
            # scheduled slot during downtime and need to fire once.
            try:
                coalesced = await self.coalesce_due(session, kind, now)
            except Exception:
                logger.exception("tick: coalesce_due failed kind=%s", kind.value)
                continue

            if coalesced:
                logger.info(
                    "tick: kind=%s coalesced missed run after downtime", kind.value
                )
                due.append(kind)

        return due
