"""Async job worker with heartbeat, checkpoint recovery, and lease management (T018)."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audr.jobs.store import (
    JobKind,
    claim_job,
    complete_job,
    fail_job,
    heartbeat,
)

logger = logging.getLogger(__name__)

_HEARTBEAT_INTERVAL_S = 60.0  # send heartbeat every 60 s
JobHandler = Callable[[AsyncSession, uuid.UUID], Awaitable[None]]


class Worker:
    """Single-threaded async worker that claims and executes jobs of a given kind."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        kind: JobKind,
        handler: JobHandler,
        worker_id: str | None = None,
        max_retries: int = 3,
        heartbeat_interval: float = _HEARTBEAT_INTERVAL_S,
    ) -> None:
        self._factory = session_factory
        self._kind = kind
        self._handler = handler
        self._worker_id = worker_id or str(uuid.uuid4())
        self._max_retries = max_retries
        self._heartbeat_interval = heartbeat_interval
        self._stop_event: asyncio.Event = asyncio.Event()

    def stop(self) -> None:
        self._stop_event.set()

    async def run_once(self) -> bool:
        """Claim and execute one job.  Returns True if a job was processed."""
        async with self._factory() as session:
            run_id = await claim_job(
                session,
                kind=self._kind,
                max_retries=self._max_retries,
                worker_id=self._worker_id,
            )
            if run_id is None:
                return False
            await session.commit()

        try:
            await self._execute_with_heartbeat(run_id)
        except Exception:
            logger.exception(
                "job failed kind=%s run_id=%s worker=%s",
                self._kind.value,
                run_id,
                self._worker_id,
            )
            return True
        return True

    async def _execute_with_heartbeat(self, run_id: uuid.UUID) -> None:
        heartbeat_task: asyncio.Task[None] | None = None
        try:
            heartbeat_task = asyncio.create_task(
                self._send_heartbeats(run_id), name=f"heartbeat-{run_id}"
            )
            async with self._factory() as session:
                await self._handler(session, run_id)
                await complete_job(session, run_id=run_id)
                await session.commit()
        except Exception as exc:
            async with self._factory() as session:
                await fail_job(session, run_id=run_id, error=str(exc))
                await session.commit()
            raise
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass

    async def _send_heartbeats(self, run_id: uuid.UUID) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_interval)
            try:
                async with self._factory() as session:
                    await heartbeat(session, run_id=run_id)
                    await session.commit()
            except Exception:
                logger.warning("heartbeat failed run_id=%s", run_id, exc_info=True)
