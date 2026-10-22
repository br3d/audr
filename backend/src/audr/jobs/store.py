"""PostgreSQL job claim/fencing, heartbeats, checkpointed recovery (T018).

Claim semantics:
  - Only one in_progress run per job kind (enforced by a partial unique index).
  - A stale lease (heartbeat_at older than LEASE_TIMEOUT) is terminated as
    `failed` (or `dead_letter` once a kind's consecutive lease-timeout count
    exceeds its max_retries) — never recycled back to `pending`, which would
    let it bypass the schedule gate and backoff streak below (AUD-689).
  - claim_job returns the new run ID or None if the kind is already active.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

_LEASE_TIMEOUT_INTERVAL = "5 minutes"
_WORKER_STALE_MINUTES = 10

# Retry backoff (AUD-356): a consecutive-failure streak delays the next claim
# instead of blocking it forever. Delay doubles per failure beyond the first,
# capped at _RETRY_BACKOFF_MAX_S so a broken provider is hammered less often
# but a job kind always eventually retries on its own — no DB intervention.
_RETRY_BACKOFF_BASE_S = 30
_RETRY_BACKOFF_MAX_S = 900  # 15 minutes
# Bounds how far back the consecutive-failure streak is counted from. Not the
# backoff mechanism itself (the cap above already guarantees self-healing) —
# just keeps the COUNT query cheap and lets a streak from a day-old incident
# stop influencing today's backoff.
_RETRY_STREAK_LOOKBACK_INTERVAL = "24 hours"

# Marks a `failed` row as caused by a lease timeout rather than a caught
# exception, so _expire_stale_leases can count *consecutive lease-timeout*
# deaths for a kind separately from ordinary (caught, reported) failures.
# Ordinary failures keep AUD-356's "always eventually retries" guarantee —
# only a run of lease-timeout deaths (the OOM/SIGKILL poison-pill signature,
# since the process never got a chance to call fail_job itself) is bounded
# toward the dead_letter terminal state (AUD-689).
_LEASE_EXPIRED_ERROR_PREFIX = "lease expired:"

# Fallback cadence (AUD-356) for a kind with no `schedule` row. The old
# behaviour ("no row = always due") is what hot-looped the worker into ~2
# claims/sec once the schedule table turned out to be empty for every kind.
# Keyed defaults mirror the frontend's SchedulesPage fallback display values;
# _DEFAULT_FRESHNESS_FALLBACK_S covers any kind not listed (e.g. a new job
# kind added before its schedule row is seeded).
# discovery and quote_refresh were raised from 1h/5min to 24h/1h (AUD-366) to
# cut down how fast a single configured RPC/quote provider's quota gets burned.
_DEFAULT_FRESHNESS_S: dict[str, int] = {
    "balance_scan": 3600,
    "discovery": 86400,
    "quote_refresh": 3600,
    "event_indexer": 300,
    "news_refresh": 900,
    "asset_icon_refresh": 900,
    "news_analyze": 300,
}
_DEFAULT_FRESHNESS_FALLBACK_S = 300


class JobKind(StrEnum):
    BALANCE_SCAN = "balance_scan"
    QUOTE_REFRESH = "quote_refresh"
    DISCOVERY = "discovery"
    VALUATION = "valuation"
    VALIDATE_RPC = "validate_rpc"
    VALIDATE_QUOTES = "validate_quotes"
    EVENT_INDEXER = "event_indexer"
    NEWS_REFRESH = "news_refresh"
    ASSET_ICON_REFRESH = "asset_icon_refresh"
    NEWS_ANALYZE = "news_analyze"


class JobRunStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    # Terminal: a lease kept expiring (no heartbeat) until the row's own
    # max_retries budget was exhausted — a poison-pill job (e.g. one that
    # OOM/SIGKILLs the worker before it can report a failure) stops being
    # re-leased forever instead of hot-looping the worker (AUD-689).
    DEAD_LETTER = "dead_letter"


# A claim_job-started run (schedule-driven); retried by claim_job's own
# consecutive-failure backoff streak across fresh rows, never requeued here.
_ORIGIN_SCHEDULED = "scheduled"
# An enqueue_job-started run (an interactive "Run now" / per-wallet request);
# a transient failure auto-requeues a fresh pending row (bounded by
# max_retries) instead of silently dropping the user's click (AUD-689).
_ORIGIN_ON_DEMAND = "on_demand"


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
    params: dict[str, Any] | None
    origin: str


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
        sa.text("SELECT id FROM job_run WHERE kind = :kind AND status = 'in_progress' LIMIT 1"),
        {"kind": kind.value},
    )
    if active.first() is not None:
        return None

    # Retry backoff: a streak of consecutive failures since the last success
    # delays the next claim with exponential backoff instead of blocking the
    # kind forever (AUD-356). _RETRY_STREAK_LOOKBACK_INTERVAL just bounds the
    # query; the backoff cap below is what guarantees self-healing.
    streak_row = (
        await session.execute(
            sa.text(
                f"""
                SELECT COUNT(*), MAX(completed_at) FROM job_run
                WHERE kind = :kind AND status = 'failed'
                  AND created_at > COALESCE(
                    (SELECT MAX(created_at) FROM job_run
                      WHERE kind = :kind AND status = 'completed'),
                    '-infinity'::timestamptz
                  )
                  AND created_at > now() - interval '{_RETRY_STREAK_LOOKBACK_INTERVAL}'
                """  # noqa: S608 — _RETRY_STREAK_LOOKBACK_INTERVAL is a module constant
            ),
            {"kind": kind.value},
        )
    ).one()
    streak, last_failed_at = streak_row
    if streak and last_failed_at is not None:
        backoff_s = min(
            _RETRY_BACKOFF_BASE_S * (2 ** min(streak - 1, max_retries)),
            _RETRY_BACKOFF_MAX_S,
        )
        if (datetime.now(tz=UTC) - last_failed_at).total_seconds() < backoff_s:
            return None

    # Schedule gate: if a schedule row exists, honour its enabled/paused/freshness
    # and next_run_at fields. A missing schedule row falls back to a conservative
    # in-code default interval, gated against this kind's most recent run — an
    # unconditional "always allowed" is what let the schedule table being empty
    # turn into a ~2-claims/sec hot loop (AUD-356).
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
    now_ts = datetime.now(tz=UTC)
    if sched_row is not None:
        enabled, paused_at, freshness_s, last_run_at, next_run_at = sched_row
        if not enabled or paused_at is not None:
            return None
        if next_run_at is not None and next_run_at > now_ts:
            return None
        if freshness_s is not None and last_run_at is not None:
            elapsed = int((now_ts - last_run_at).total_seconds())
            if elapsed < freshness_s:
                return None
    else:
        default_freshness_s = _DEFAULT_FRESHNESS_S.get(kind.value, _DEFAULT_FRESHNESS_FALLBACK_S)
        last_run = await session.execute(
            sa.text(
                """
                SELECT MAX(COALESCE(completed_at, created_at)) FROM job_run
                WHERE kind = :kind AND status IN ('completed', 'failed')
                """
            ),
            {"kind": kind.value},
        )
        last_run_at = last_run.scalar()
        if last_run_at is not None:
            elapsed = int((now_ts - last_run_at).total_seconds())
            if elapsed < default_freshness_s:
                return None

    # Insert a new in_progress run.
    run_id = uuid.uuid4()
    now = datetime.now(tz=UTC)
    await session.execute(
        sa.text(
            """
            INSERT INTO job_run
              (id, kind, status, worker_id, max_retries, claimed_at, heartbeat_at, origin)
            VALUES
              (:id, :kind, 'in_progress', :worker_id, :max_retries, :now, :now, :origin)
            """
        ),
        {
            "id": run_id,
            "kind": kind.value,
            "worker_id": worker_id,
            "max_retries": max_retries,
            "now": now,
            "origin": _ORIGIN_SCHEDULED,
        },
    )
    await session.flush()
    return run_id


async def enqueue_job(
    session: AsyncSession,
    *,
    kind: JobKind,
    max_retries: int = 3,
    params: dict[str, Any] | None = None,
    _retry_count: int = 0,
) -> uuid.UUID:
    """Insert a ``pending`` job run as an on-demand queue entry.

    Unlike :func:`claim_job` (which starts a run immediately as ``in_progress``),
    this only records a request.  A worker running :func:`claim_pending_job`
    picks it up on its next poll and executes it.  Used for interactively
    triggered jobs such as integration validation (AUD-313), and for
    per-wallet-scoped balance_scan/discovery requests (AUD-399/AUD-400) via
    ``params={"wallet_id": ...}``.

    ``_retry_count`` is an internal knob for :func:`fail_job` /
    :func:`_expire_stale_leases` to carry an on-demand request's attempt
    count forward onto its replacement row when auto-requeuing (AUD-689) —
    external callers should never pass it.
    """
    run_id = uuid.uuid4()
    await session.execute(
        sa.text(
            "INSERT INTO job_run (id, kind, status, max_retries, params, retry_count, origin)"
            " VALUES (:id, :kind, 'pending', :max_retries, CAST(:params AS jsonb),"
            " :retry_count, :origin)"
        ),
        {
            "id": run_id,
            "kind": kind.value,
            "max_retries": max_retries,
            "params": json.dumps(params) if params is not None else None,
            "retry_count": _retry_count,
            "origin": _ORIGIN_ON_DEMAND,
        },
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
        sa.text("SELECT id FROM job_run WHERE kind = :kind AND status = 'in_progress' LIMIT 1"),
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
    stale = heartbeat is None or datetime.now(tz=UTC) - heartbeat > timedelta(
        minutes=_WORKER_STALE_MINUTES
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
    """Mark *run_id* as failed, increment retry_count, and retry it if on-demand.

    A ``scheduled``-origin failure (a ``claim_job`` run) relies on the
    existing AUD-356 backoff streak across fresh rows to self-heal — nothing
    more to do here.  An ``on_demand``-origin failure (an interactively
    triggered "Run now" / per-wallet request) has no such mechanism: without
    a retry, a transient failure drops the user's click silently (AUD-689).
    If the row's own retry budget isn't exhausted, requeue a fresh pending
    row carrying the attempt count and params forward so the next worker
    poll picks it back up.
    """
    row = (
        await session.execute(
            sa.text(
                """
                UPDATE job_run
                SET status = 'failed',
                    error = :error,
                    checkpoint = CAST(:checkpoint AS jsonb),
                    retry_count = retry_count + 1,
                    completed_at = now()
                WHERE id = :id AND status = 'in_progress'
                RETURNING kind, origin, max_retries, retry_count, params
                """
            ),
            {
                "id": run_id,
                "error": error,
                "checkpoint": json.dumps(checkpoint) if checkpoint is not None else None,
            },
        )
    ).first()
    await session.flush()
    if row is None:
        return
    kind, origin, max_retries, retry_count, params = row
    if origin == _ORIGIN_ON_DEMAND and retry_count < max_retries:
        await enqueue_job(
            session,
            kind=JobKind(kind),
            max_retries=max_retries,
            params=dict(params) if params is not None else None,
            _retry_count=retry_count,
        )


async def set_checkpoint(
    session: AsyncSession, *, run_id: uuid.UUID, checkpoint: dict[str, Any]
) -> None:
    """Persist *checkpoint* on *run_id*, independent of its terminal status.

    Unlike :func:`fail_job`'s checkpoint (scoped to failed runs, for retry
    resumption), this lets a run that still completes successfully record
    diagnostic counts — e.g. balance_scan's attempted/failed read tally
    (AUD-463) — so an operator can tell a degraded-but-tolerable run from a
    fully clean one instead of both landing as indistinguishable `completed`
    rows.
    """
    await session.execute(
        sa.text("UPDATE job_run SET checkpoint = CAST(:checkpoint AS jsonb) WHERE id = :id"),
        {"id": run_id, "checkpoint": json.dumps(checkpoint)},
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


async def get_job_run(session: AsyncSession, *, run_id: uuid.UUID) -> JobRun | None:
    """Fetch a job run by ID or return None."""
    result = await session.execute(
        sa.text(
            """
            SELECT id, kind, status, retry_count, max_retries, error,
                   checkpoint, claimed_at, heartbeat_at, completed_at, created_at,
                   params, origin
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
        params=row[11],
        origin=row[12],
    )


async def get_job_params(session: AsyncSession, *, run_id: uuid.UUID) -> dict[str, Any] | None:
    """Return the ``params`` blob stored on *run_id*, or None if unset."""
    result = await session.execute(
        sa.text("SELECT params FROM job_run WHERE id = :id"),
        {"id": run_id},
    )
    row = result.first()
    if row is None or row[0] is None:
        return None
    return dict(row[0])


async def _expire_stale_leases(session: AsyncSession, *, kind: JobKind) -> None:
    """Terminate (not recycle) an in_progress run whose heartbeat has expired.

    See the module docstring and AUD-689 for why this no longer sets
    ``status = 'pending'``: that let an expired run bypass both the
    schedule gate and the backoff streak by sitting in the queue
    :func:`claim_pending_job` serves unconditionally on every poll.

    The partial unique index (``uq_job_run_kind_active``) guarantees at
    most one ``in_progress`` row per kind, so at most one row is affected.
    """
    # _LEASE_TIMEOUT_INTERVAL is a module constant, not user input — safe to embed.
    stale = (
        await session.execute(
            sa.text(
                f"""
                SELECT id, max_retries FROM job_run
                WHERE kind = :kind AND status = 'in_progress'
                  AND heartbeat_at < now() - interval '{_LEASE_TIMEOUT_INTERVAL}'
                LIMIT 1
                """  # noqa: S608 — _LEASE_TIMEOUT_INTERVAL is a module constant, not user input
            ),
            {"kind": kind.value},
        )
    ).first()
    if stale is None:
        return
    run_id, max_retries = stale

    # Consecutive lease-timeout deaths for this kind since its last success —
    # deliberately narrower than claim_job's own backoff streak (which counts
    # *every* failed row, lease timeouts included, and keeps pacing retries
    # forever per AUD-356). This one exists only to recognise a poison pill
    # that never gets the chance to call fail_job itself.
    streak = (
        await session.execute(
            sa.text(
                """
                SELECT COUNT(*) FROM job_run
                WHERE kind = :kind AND status = 'failed' AND error LIKE :marker
                  AND created_at > COALESCE(
                    (SELECT MAX(created_at) FROM job_run
                      WHERE kind = :kind AND status = 'completed'),
                    '-infinity'::timestamptz
                  )
                """
            ),
            {"kind": kind.value, "marker": f"{_LEASE_EXPIRED_ERROR_PREFIX}%"},
        )
    ).scalar_one()

    exhausted = (streak + 1) >= max_retries
    status = JobRunStatus.DEAD_LETTER.value if exhausted else JobRunStatus.FAILED.value
    error = (
        f"{_LEASE_EXPIRED_ERROR_PREFIX} retry budget exhausted after {streak + 1}"
        " consecutive lease timeouts"
        if exhausted
        else f"{_LEASE_EXPIRED_ERROR_PREFIX} heartbeat timeout"
    )

    updated = (
        await session.execute(
            sa.text(
                """
                UPDATE job_run
                SET status = :status,
                    error = :error,
                    worker_id = NULL,
                    retry_count = retry_count + 1,
                    completed_at = now()
                WHERE id = :id AND status = 'in_progress'
                RETURNING kind, origin, max_retries, retry_count, params
                """
            ),
            {"id": run_id, "status": status, "error": error},
        )
    ).first()
    await session.flush()
    if updated is None or exhausted:
        return

    # Under budget: an on_demand-origin run (an interactive "Run now" / per-
    # wallet request) is requeued immediately so the user's click isn't lost
    # (AUD-689 point 4). A scheduled-origin run relies on claim_job's own
    # backoff streak — now correctly fed by the 'failed' row above — to
    # start a fresh attempt.
    kind_db, origin, max_retries_row, retry_count, params = updated
    if origin == _ORIGIN_ON_DEMAND and retry_count < max_retries_row:
        await enqueue_job(
            session,
            kind=JobKind(kind_db),
            max_retries=max_retries_row,
            params=dict(params) if params is not None else None,
            _retry_count=retry_count,
        )
