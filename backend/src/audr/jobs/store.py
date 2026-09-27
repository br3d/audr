"""PostgreSQL job claim/fencing, heartbeats, checkpointed recovery (T018).

Claim semantics:
  - Only one in_progress run per job kind (enforced by a partial unique index).
  - Stale leases (heartbeat_at older than LEASE_TIMEOUT) are eligible for re-claim.
  - claim_job returns the new run ID or None if the kind is already active.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

_LEASE_TIMEOUT_INTERVAL = "5 minutes"


class JobKind(str, enum.Enum):
    BALANCE_SCAN = "balance_scan"
    QUOTE_REFRESH = "quote_refresh"
    DISCOVERY = "discovery"
    VALUATION = "valuation"


class JobRunStatus(str, enum.Enum):
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

    Returns the new run UUID on success, or None if an active lease already exists
    or the retry budget is exhausted.
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

    # Check retry budget: count recent failed runs.
    exhausted = await session.execute(
        sa.text(
            """
            SELECT COUNT(*) FROM job_run
            WHERE kind = :kind
              AND status = 'failed'
              AND retry_count >= max_retries
            """
        ),
        {"kind": kind.value},
    )
    if (exhausted.scalar() or 0) > 0:
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


async def heartbeat(session: AsyncSession, *, run_id: uuid.UUID) -> None:
    """Advance the heartbeat timestamp for *run_id* to prevent lease expiry."""
    await session.execute(
        sa.text(
            "UPDATE job_run SET heartbeat_at = now() WHERE id = :id AND status = 'in_progress'"
        ),
        {"id": run_id},
    )
    await session.flush()


async def complete_job(session: AsyncSession, *, run_id: uuid.UUID) -> None:
    """Mark *run_id* as completed."""
    await session.execute(
        sa.text(
            """
            UPDATE job_run
            SET status = 'completed', completed_at = now()
            WHERE id = :id AND status = 'in_progress'
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
            WHERE id = :id
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
