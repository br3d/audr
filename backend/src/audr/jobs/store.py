"""PostgreSQL job claim/fencing, heartbeats, checkpointed recovery (T018).

Claim semantics:
  - Only one in_progress run per job kind (enforced by a partial unique index).
  - Stale leases (heartbeat_at older than LEASE_TIMEOUT) are eligible for re-claim.
  - claim_job returns the new run ID or None if the kind is already active.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

_LEASE_TIMEOUT_INTERVAL = "5 minutes"
_WORKER_STALE_MINUTES = 10


class JobKind(StrEnum):
    BALANCE_SCAN = "balance_scan"
    QUOTE_REFRESH = "quote_refresh"
    DISCOVERY = "discovery"
    VALUATION = "valuation"
    VALIDATE_RPC = "validate_rpc"
    VALIDATE_QUOTES = "validate_quotes"
    EVENT_INDEXER = "event_indexer"
    NEWS_REFRESH = "news_refresh"


class JobRunStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class JobRun:
    id: uuid.UUID
    kind: str
    status: JobRunStatus
    retry_count: int
    max_retries: int
    error: str | None
    checkpoint: dict[str, Any] | None
    claimed_at: datetime | None
    heartbeat_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


async def claim_job(
    session: AsyncSession,
    *,
    kind: JobKind,
    max_retries: int = 3,
    worker_id: str | None = None,
) -> uuid.UUID | None:
    """Attempt to claim a job of *kind*.

    Returns the new run UUID on success, or None if an active lease already exists,
    the retry budget is exhausted, or the schedule says the job is not yet due.
    """
    # Expire any stale leases before trying to claim.
    await _expire_stale_leases(session, kind=kind)

    # Check if an active lease exists (the partial unique index makes this atomic).
    active = await session.execute(
        sa.text(
            "SELECT id FROM job_run WHERE kind = :kind AND status = 'in_progress' LIMIT 1"
        ),
        {"kind": kind.value},
    )
    if active.first() is not None:
        return None

    # Check retry budget: count consecutive failures since the last success.
    # This prevents transient failures from permanently blocking a job kind.
    exhausted = await session.execute(
        sa.text(
            """
            SELECT COUNT(*) FROM job_run
            WHERE kind = :kind AND status = 'failed'
              AND created_at > COALESCE(
                (SELECT MAX(created_at) FROM job_run
                  WHERE kind = :kind AND status = 'completed'),
                '-infinity'::timestamptz
              )
            """
        ),
        {"kind": kind.value},
    )
    if (exhausted.scalar() or 0) >= max_retries:
        return None

    # Schedule gate: if a schedule row exists, honour its enabled/paused/freshness
    # and next_run_at fields.  A missing schedule row means "always allowed".
    sched = await session.execute(
        sa.text(
            """
            SELECT enabled, paused_at, freshness_s, last_run_at, next_run_at
            FROM schedule WHERE kind = :kind
            """
        ),
        {"kind": kind.value},
    )
    sched_row = sched.first()
    if sched_row is not None:
        enabled, paused_at, freshness_s, last_run_at, next_run_at = sched_row
        if not enabled or paused_at is not None:
            return None
        now_ts = datetime.now(tz=UTC)
        if next_run_at is not None and next_run_at > now_ts:
            return None
        if freshness_s is not None and last_run_at is not None:
            elapsed = int((now_ts - last_run_at).total_seconds())
            if elapsed < freshness_s:
                return None

    # Job fencing: QUOTE_REFRESH requires a live coingecko integration.
    if kind == JobKind.QUOTE_REFRESH:
        guard = await session.execute(
            sa.text("SELECT 1 FROM integration WHERE kind = 'coingecko' LIMIT 1"),
        )
        if guard.first() is None:
            return None

    # Insert a new in_progress run.
    run_id = uuid.uuid4()
    now = datetime.now(tz=UTC)
    await session.execute(
        sa.text(
            """
            INSERT INTO job_run
              (id, kind, status, worker_id, max_retries, claimed_at, heartbeat_at)
            VALUES
              (:id, :kind, 'in_progress', :worker_id, :max_retries, :now, :now)
            """
        ),
        {
            "id": run_id,
            "kind": kind.value,
            "worker_id": worker_id,
            "max_retries": max_retries,
            "now": now,
        },
    )
    await session.flush()
    return run_id


async def enqueue_job(
    session: AsyncSession,
    *,
    kind: JobKind,
    max_retries: int = 3,
) -> uuid.UUID:
    """Insert a ``pending`` job run as an on-demand queue entry.

    Unlike :func:`claim_job` (which starts a run immediately as ``in_progress``),
    this only records a request.  A worker running :func:`claim_pending_job`
    picks it up on its next poll and executes it.  Used for interactively
    triggered jobs such as integration validation (AUD-313).
    """
    run_id = uuid.uuid4()
    await session.execute(
        sa.text(
            "INSERT INTO job_run (id, kind, status, max_retries)"
            " VALUES (:id, :kind, 'pending', :max_retries)"
        ),
        {"id": run_id, "kind": kind.value, "max_retries": max_retries},
    )
    await session.flush()
    return run_id


async def claim_pending_job(
    session: AsyncSession,
    *,
    kind: JobKind,
    worker_id: str | None = None,
) -> uuid.UUID | None:
    """Claim the oldest ``pending`` run of *kind* and move it to ``in_progress``.

    For on-demand jobs (validation) whose queue entries are created by
    :func:`enqueue_job`.  Returns the resumed run ID, or None if nothing is
    pending or a run of this kind is already active.  Never inserts a new run,
    so the worker stays idle until a request is actually enqueued.
    """
    # Reclaim any lease abandoned by a crashed worker before checking activity.
    await _expire_stale_leases(session, kind=kind)

    active = await session.execute(
        sa.text(
            "SELECT id FROM job_run WHERE kind = :kind AND status = 'in_progress' LIMIT 1"
        ),
        {"kind": kind.value},
    )
    if active.first() is not None:
        return None

    now = datetime.now(tz=UTC)
    claimed = await session.execute(
        sa.text(
            """
            UPDATE job_run
            SET status = 'in_progress',
                worker_id = :worker_id,
                claimed_at = :now,
                heartbeat_at = :now
            WHERE id = (
                SELECT id FROM job_run
                WHERE kind = :kind AND status = 'pending'
                ORDER BY created_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id
            """
        ),
        {"kind": kind.value, "worker_id": worker_id, "now": now},
    )
    row = claimed.first()
    if row is None:
        return None
    await session.flush()
    return row[0]


async def heartbeat(session: AsyncSession, *, run_id: uuid.UUID) -> None:
    """Advance the heartbeat timestamp for *run_id* to prevent lease expiry."""
    await session.execute(
        sa.text(
            "UPDATE job_run SET heartbeat_at = now() WHERE id = :id AND status = 'in_progress'"
        ),
        {"id": run_id},
    )
    await session.flush()


async def upsert_worker_status(
    session: AsyncSession,
    *,
    worker_id: str,
    status: str,
    current_job_run_id: uuid.UUID | None = None,
) -> None:
    """Record a worker-process liveness heartbeat (AUD-318).

    Nothing wrote `worker_status` before this, so `GET /api/v1/status` reported
    the worker as "unknown" forever and the Status page could not distinguish a
    healthy worker from a dead one. `status` must be one of idle/running/stopped
    (enforced by ck_worker_status_ck_worker_status_status).
    """
    await session.execute(
        sa.text(
            """
            INSERT INTO worker_status (worker_id, status, last_heartbeat_at, current_job_run_id)
            VALUES (:worker_id, :status, now(), :run_id)
            ON CONFLICT (worker_id) DO UPDATE
            SET status = EXCLUDED.status,
                last_heartbeat_at = EXCLUDED.last_heartbeat_at,
                current_job_run_id = EXCLUDED.current_job_run_id
            """
        ),
        {"worker_id": worker_id, "status": status, "run_id": current_job_run_id},
    )
    await session.flush()


async def get_worker_heartbeat(session: AsyncSession) -> tuple[str, datetime | None]:
    """Return (status, last_heartbeat_at) from the most recent worker heartbeat.

    Shared by ``GET /api/v1/status`` and ``GET /health/ready`` so both routes
    agree on one vocabulary (idle/running/stopped/unknown) and one staleness
    threshold. A heartbeat older than ``_WORKER_STALE_MINUTES`` is reported as
    "stopped" even if the row's own ``status`` still says idle/running — a
    worker process that died mid-poll never got to write "stopped" itself.
    No row at all (nothing has ever written a heartbeat) is "unknown".
    """
    row = (
        await session.execute(
            sa.text(
                "SELECT status, last_heartbeat_at FROM worker_status"
                " ORDER BY last_heartbeat_at DESC LIMIT 1"
            )
        )
    ).first()
    if row is None:
        return "unknown", None

    status, heartbeat = row
    stale = (
        heartbeat is None
        or datetime.now(tz=UTC) - heartbeat > timedelta(minutes=_WORKER_STALE_MINUTES)
    )
    if stale:
        return "stopped", heartbeat
    if status in ("idle", "running"):
        return "running", heartbeat
    return status, heartbeat


async def complete_job(session: AsyncSession, *, run_id: uuid.UUID) -> None:
    """Mark *run_id* as completed and advance the schedule's last_run_at."""
    await session.execute(
        sa.text(
            """
            WITH run AS (
                UPDATE job_run
                SET status = 'completed', completed_at = now()
                WHERE id = :id AND status = 'in_progress'
                RETURNING kind
            )
            UPDATE schedule
            SET last_run_at = now(), next_run_at = NULL
            WHERE kind = (SELECT kind FROM run)
            """
        ),
        {"id": run_id},
    )
    await session.flush()


async def fail_job(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    error: str,
    checkpoint: dict[str, Any] | None = None,
) -> None:
    """Mark *run_id* as failed and increment retry_count."""
    await session.execute(
        sa.text(
            """
            UPDATE job_run
            SET status = 'failed',
                error = :error,
                checkpoint = :checkpoint,
                retry_count = retry_count + 1,
                completed_at = now()
            WHERE id = :id AND status = 'in_progress'
            """
        ),
        {"id": run_id, "error": error, "checkpoint": checkpoint},
    )
    await session.flush()


async def release_lease(session: AsyncSession, *, run_id: uuid.UUID) -> None:
    """Release a lease without failing — moves the run back to pending."""
    await session.execute(
        sa.text(
            "UPDATE job_run SET status = 'pending', worker_id = NULL"
            " WHERE id = :id AND status = 'in_progress'"
        ),
        {"id": run_id},
    )
    await session.flush()


async def get_job_run(
    session: AsyncSession, *, run_id: uuid.UUID
) -> JobRun | None:
    """Fetch a job run by ID or return None."""
    result = await session.execute(
        sa.text(
            """
            SELECT id, kind, status, retry_count, max_retries, error,
                   checkpoint, claimed_at, heartbeat_at, completed_at, created_at
            FROM job_run WHERE id = :id
            """
        ),
        {"id": run_id},
    )
    row = result.first()
    if row is None:
        return None
    return JobRun(
        id=row[0],
        kind=row[1],
        status=JobRunStatus(row[2]),
        retry_count=row[3],
        max_retries=row[4],
        error=row[5],
        checkpoint=row[6],
        claimed_at=row[7],
        heartbeat_at=row[8],
        completed_at=row[9],
        created_at=row[10],
    )


async def _expire_stale_leases(session: AsyncSession, *, kind: JobKind) -> None:
    """Move in_progress runs whose heartbeat has expired back to pending."""
    # _LEASE_TIMEOUT_INTERVAL is a module constant, not user input — safe to embed.
    _sql = (
        f"UPDATE job_run SET status = 'pending', worker_id = NULL"  # noqa: S608
        f" WHERE kind = :kind AND status = 'in_progress'"
        f" AND heartbeat_at < now() - interval '{_LEASE_TIMEOUT_INTERVAL}'"
    )
    await session.execute(
        sa.text(_sql),
        {"kind": kind.value},
    )
