"""Job scheduler: enqueue jobs and coalesce manual triggers (T019)."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audr.jobs.store import JobKind, claim_job

logger = logging.getLogger(__name__)


class Scheduler:
    """Coalescing job scheduler.

    Manual trigger requests for the same job kind are coalesced: if a run of
    that kind is already active or queued, no extra run is started.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory
        self._pending: dict[JobKind, asyncio.Event] = defaultdict(asyncio.Event)

    async def trigger(self, kind: JobKind, *, max_retries: int = 3) -> bool:
        """Request an immediate run of *kind*.

        Returns True if a new run was claimed, False if one was already active
        (coalesced) or the retry budget is exhausted.
        """
        async with self._factory() as session:
            run_id = await claim_job(session, kind=kind, max_retries=max_retries)
            if run_id is None:
                logger.info("trigger coalesced kind=%s (already active)", kind.value)
                return False
            await session.commit()
            logger.info("trigger claimed kind=%s run_id=%s", kind.value, run_id)
            return True

    async def due_schedules(self, session: AsyncSession) -> list[JobKind]:
        """Return job kinds whose next_run_at is in the past and schedule is enabled."""
        now = datetime.now(tz=UTC)
        result = await session.execute(
            sa.text(
                """
                SELECT kind FROM schedule
                WHERE enabled = true
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
