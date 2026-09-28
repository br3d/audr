"""Authenticated settings, job list/detail/cancel, and operational status routes (T083 / US4)."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from audr.api.auth import _require_csrf, _require_session
from audr.auth.service import AuthenticationError
from audr.db import get_db
from audr.jobs.store import JobKind, claim_job
from audr.operations.exports import (
    export_current_portfolio,
    export_full_history,
    render_history_csv,
    render_portfolio_csv,
)
from audr.operations.purge import execute_purge, preview_purge
from audr.settings.schedules import (
    RevisionConflictError,
    get_schedule,
    pause_schedule,
    resume_schedule,
    update_schedule,
)

router = APIRouter(prefix="/api/v1")

# ---------------------------------------------------------------------------
# Kind mapping: DB internal names ↔ frontend API names
# ---------------------------------------------------------------------------

_DB_TO_FE: dict[str, str] = {
    "balance_scan": "balances",
    "quote_refresh": "quotes",
    "discovery": "discovery",
}
_FE_TO_DB: dict[str, str] = {v: k for k, v in _DB_TO_FE.items()}

_WORKER_STALE_MINUTES = 10


# ---------------------------------------------------------------------------
# Cron ↔ interval_seconds helpers
# ---------------------------------------------------------------------------


def _cron_to_seconds(cron_expr: str) -> int:
    """Convert a simple cron expression to an interval in seconds.

    Handles the common patterns produced by _seconds_to_cron; falls back to
    croniter for anything else.
    """
    m = re.fullmatch(r"\*/(\d+) \* \* \* \*", cron_expr)
    if m:
        return int(m.group(1)) * 60
    if cron_expr == "* * * * *":
        return 60
    if cron_expr == "0 * * * *":
        return 3600
    m = re.fullmatch(r"0 \*/(\d+) \* \* \*", cron_expr)
    if m:
        return int(m.group(1)) * 3600
    if cron_expr == "0 0 * * *":
        return 86400
    m = re.fullmatch(r"0 0 \*/(\d+) \* \*", cron_expr)
    if m:
        return int(m.group(1)) * 86400
    # Fallback: use croniter to compute one period length
    from croniter import croniter as CronIter
    it = CronIter(cron_expr, start_time=0)
    t1: float = it.get_next(float)
    t2: float = it.get_next(float)
    return max(60, round(t2 - t1))


def _seconds_to_cron(seconds: int) -> str:
    """Convert an interval in seconds to the simplest equivalent cron expression."""
    if seconds < 3600:
        mins = max(1, seconds // 60)
        return f"*/{mins} * * * *"
    if seconds < 86400:
        hours = max(1, seconds // 3600)
        return f"0 */{hours} * * *"
    days = max(1, seconds // 86400)
    return f"0 0 */{days} * *"


# ---------------------------------------------------------------------------
# Pydantic schemas — internal (per-kind schedule CRUD)
# ---------------------------------------------------------------------------


class ScheduleRead(BaseModel):
    kind: str
    cron_expr: str
    enabled: bool
    paused: bool
    revision: int
    freshness_s: int | None
    budget_calls_per_day: int | None
    last_run_at: datetime | None
    next_run_at: datetime | None


class ScheduleUpdate(BaseModel):
    cron_expr: str | None = None
    freshness_s: int | None = None
    budget_calls_per_day: int | None = None
    expected_revision: int | None = None


# ---------------------------------------------------------------------------
# Pydantic schemas — public API (SettingsResponse / StatusResponse)
# ---------------------------------------------------------------------------


class ScheduleConfig(BaseModel):
    enabled: bool
    interval_seconds: int
    freshness_seconds: int | None = None
    next_due_at: str | None = None


class SettingsResponse(BaseModel):
    revision: str
    schedules: dict[str, ScheduleConfig]


class SettingsSchedulePatch(BaseModel):
    enabled: bool | None = None
    interval_seconds: int | None = None
    freshness_seconds: int | None = None


class SettingsPatch(BaseModel):
    revision: str
    schedules: dict[str, SettingsSchedulePatch] | None = None


class DbStatus(BaseModel):
    status: str


class WorkerStatus(BaseModel):
    status: str
    last_heartbeat_at: str | None = None


class RecoveryStatus(BaseModel):
    active: bool
    reason: str | None = None


class StatusResponse(BaseModel):
    db: DbStatus
    worker: WorkerStatus
    recovery: RecoveryStatus
    schedules: dict[str, ScheduleConfig]
    version: str | None = None


# ---------------------------------------------------------------------------
# Pydantic schemas — jobs
# ---------------------------------------------------------------------------


class JobRunResponse(BaseModel):
    id: str
    kind: str
    status: str
    started_at: str | None
    finished_at: str | None
    attempted: int
    succeeded: int
    failed: int
    error_message: str | None
    created_at: str


class JobsListResponse(BaseModel):
    items: list[JobRunResponse]
    next_cursor: str | None = None
    request_id: str
    generated_at: str


class JobRef(BaseModel):
    run_id: str
    coalesced: bool


class TriggerJobInput(BaseModel):
    kind: str


# ---------------------------------------------------------------------------
# Internal helpers — kept for per-kind schedule CRUD routes
# ---------------------------------------------------------------------------

_SELECT_SCHEDULE_COLS = """
    id, kind, cron_expr, enabled, revision, paused_at,
    freshness_s, budget_calls_per_day, last_run_at, next_run_at, updated_at
"""


def _row_to_schedule_read(row: Any) -> ScheduleRead:  # noqa: ANN401
    return ScheduleRead(
        kind=row[1],
        cron_expr=row[2],
        enabled=row[3],
        paused=row[5] is not None,
        revision=row[4],
        freshness_s=row[6],
        budget_calls_per_day=row[7],
        last_run_at=row[8],
        next_run_at=row[9],
    )


def _dict_to_schedule_read(d: dict) -> ScheduleRead:  # type: ignore[type-arg]
    return ScheduleRead(
        kind=d["kind"],
        cron_expr=d["cron_expr"],
        enabled=d["enabled"],
        paused=d["paused_at"] is not None,
        revision=d["revision"],
        freshness_s=d["freshness_s"],
        budget_calls_per_day=d["budget_calls_per_day"],
        last_run_at=d["last_run_at"],
        next_run_at=d["next_run_at"],
    )


def _row_to_schedule_config(row: Any) -> tuple[str, ScheduleConfig]:  # noqa: ANN401
    """Return (frontend_kind, ScheduleConfig) from a schedule table row."""
    kind_db: str = row[1]
    cron_expr: str = row[2]
    enabled: bool = row[3]
    freshness_s: int | None = row[6]
    next_run_at: datetime | None = row[9]

    kind_fe = _DB_TO_FE.get(kind_db, kind_db)
    interval_s = _cron_to_seconds(cron_expr)
    next_due = next_run_at.isoformat() if next_run_at else None

    return kind_fe, ScheduleConfig(
        enabled=enabled,
        interval_seconds=interval_s,
        freshness_seconds=freshness_s,
        next_due_at=next_due,
    )


def _map_job_row(row: Any) -> JobRunResponse:  # noqa: ANN401
    """Map a job_run table row to the frontend-shaped JobRunResponse."""
    job_id, kind_db, status, retry_count, error, claimed_at, completed_at, created_at = row

    kind_fe = _DB_TO_FE.get(kind_db, kind_db)
    status_fe = "running" if status == "in_progress" else status

    attempted = retry_count + (0 if status == "pending" else 1)
    succeeded = 1 if status == "completed" else 0

    return JobRunResponse(
        id=str(job_id),
        kind=kind_fe,
        status=status_fe,
        started_at=claimed_at.isoformat() if claimed_at else None,
        finished_at=completed_at.isoformat() if completed_at else None,
        attempted=attempted,
        succeeded=succeeded,
        failed=retry_count,
        error_message=error,
        created_at=created_at.isoformat(),
    )


async def _query_settings_response(db: AsyncSession) -> SettingsResponse:
    """Build the SettingsResponse from the current schedule table state."""
    result = await db.execute(
        sa.text(f"SELECT {_SELECT_SCHEDULE_COLS} FROM schedule ORDER BY kind")  # noqa: S608
    )
    rows = result.fetchall()

    schedules: dict[str, ScheduleConfig] = {}
    max_revision = 0
    for row in rows:
        revision: int = row[4]
        kind_fe, config = _row_to_schedule_config(row)
        schedules[kind_fe] = config
        max_revision = max(max_revision, revision)

    return SettingsResponse(revision=str(max_revision), schedules=schedules)


# ---------------------------------------------------------------------------
# Routes — batch settings (frontend contract)
# ---------------------------------------------------------------------------


@router.get("/settings", response_model=SettingsResponse)
async def get_settings(
    _session: Annotated[Any, Depends(_require_session)],
    db: AsyncSession = Depends(get_db),
) -> SettingsResponse:
    """Return all schedule settings in the frontend-expected shape."""
    return await _query_settings_response(db)


@router.patch("/settings", response_model=SettingsResponse)
async def patch_settings(
    body: SettingsPatch,
    _session: Annotated[Any, Depends(_require_csrf)],
    db: AsyncSession = Depends(get_db),
) -> SettingsResponse:
    """Update schedule settings (optimistic-lock on revision)."""
    # Check global revision
    rev_result = await db.execute(sa.text("SELECT MAX(revision) FROM schedule"))
    current_revision: int = rev_result.scalar() or 0
    if str(current_revision) != body.revision:
        raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail="revision conflict")

    if body.schedules:
        for kind_fe, patch in body.schedules.items():
            kind_db = _FE_TO_DB.get(kind_fe, kind_fe)
            cron_expr: str | None = None
            if patch.interval_seconds is not None:
                cron_expr = _seconds_to_cron(patch.interval_seconds)
            freshness_s = patch.freshness_seconds

            sets: list[str] = ["revision = revision + 1"]
            params: dict[str, Any] = {"kind": kind_db}

            if patch.enabled is not None:
                sets.append("enabled = :enabled")
                params["enabled"] = patch.enabled
            if cron_expr is not None:
                sets.append("cron_expr = :cron_expr")
                params["cron_expr"] = cron_expr
            if freshness_s is not None:
                sets.append("freshness_s = :freshness_s")
                params["freshness_s"] = freshness_s

            if len(sets) > 1:  # more than just the revision bump
                await db.execute(
                    sa.text(
                        "UPDATE schedule SET "  # noqa: S608
                        + ", ".join(sets)
                        + " WHERE kind = :kind"
                    ),
                    params,
                )

    await db.commit()
    return await _query_settings_response(db)


# ---------------------------------------------------------------------------
# Routes — per-kind schedule CRUD (internal / power-user)
# ---------------------------------------------------------------------------


@router.get("/settings/schedules/{kind}", response_model=ScheduleRead)
async def get_schedule_route(
    kind: str,
    _session: Annotated[Any, Depends(_require_session)],
    db: AsyncSession = Depends(get_db),
) -> ScheduleRead:
    """Return a single schedule by kind, or 404."""
    d = await get_schedule(db, kind=kind)
    if d is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="schedule not found",
        )
    return _dict_to_schedule_read(d)


@router.patch("/settings/schedules/{kind}", response_model=ScheduleRead)
async def patch_schedule(
    kind: str,
    body: ScheduleUpdate,
    _session: Annotated[Any, Depends(_require_csrf)],
    db: AsyncSession = Depends(get_db),
) -> ScheduleRead:
    """Update configurable schedule fields."""
    try:
        d = await update_schedule(
            db,
            kind=kind,
            cron_expr=body.cron_expr,
            freshness_s=body.freshness_s,
            budget_calls_per_day=body.budget_calls_per_day,
            expected_revision=body.expected_revision,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except RevisionConflictError:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="revision conflict",
        ) from None
    except LookupError:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="schedule not found",
        ) from None
    await db.commit()
    return _dict_to_schedule_read(d)


@router.post("/settings/schedules/{kind}/pause", response_model=ScheduleRead)
async def pause_schedule_route(
    kind: str,
    _session: Annotated[Any, Depends(_require_csrf)],
    db: AsyncSession = Depends(get_db),
) -> ScheduleRead:
    """Pause a schedule (sets paused_at = now())."""
    try:
        d = await pause_schedule(db, kind=kind)
    except LookupError:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="schedule not found",
        ) from None
    await db.commit()
    return _dict_to_schedule_read(d)


@router.post("/settings/schedules/{kind}/resume", response_model=ScheduleRead)
async def resume_schedule_route(
    kind: str,
    _session: Annotated[Any, Depends(_require_csrf)],
    db: AsyncSession = Depends(get_db),
) -> ScheduleRead:
    """Resume a paused schedule (clears paused_at)."""
    try:
        d = await resume_schedule(db, kind=kind)
    except LookupError:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="schedule not found",
        ) from None
    await db.commit()
    return _dict_to_schedule_read(d)


# ---------------------------------------------------------------------------
# Routes — jobs (frontend contract)
# ---------------------------------------------------------------------------


@router.post("/jobs", response_model=JobRef)
async def trigger_job(
    body: TriggerJobInput,
    _session: Annotated[Any, Depends(_require_csrf)],
    db: AsyncSession = Depends(get_db),
) -> JobRef:
    """Queue an immediate job run, coalescing if one is already active."""
    kind_db = _FE_TO_DB.get(body.kind)
    if kind_db is None:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown kind: {body.kind!r}",
        )
    try:
        job_kind = JobKind(kind_db)
    except ValueError:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unsupported kind: {body.kind!r}",
        ) from None

    # Check for already-active run first so we can return its ID.
    active_result = await db.execute(
        sa.text("SELECT id FROM job_run WHERE kind = :kind AND status = 'in_progress' LIMIT 1"),
        {"kind": kind_db},
    )
    active_row = active_result.first()
    if active_row is not None:
        return JobRef(run_id=str(active_row[0]), coalesced=True)

    run_id = await claim_job(db, kind=job_kind)
    if run_id is None:
        # Coalesced by a concurrent claim between our check and claim_job.
        existing = await db.execute(
            sa.text("SELECT id FROM job_run WHERE kind = :kind AND status = 'in_progress' LIMIT 1"),
            {"kind": kind_db},
        )
        ex_row = existing.first()
        existing_id = str(ex_row[0]) if ex_row else str(uuid.uuid4())
        return JobRef(run_id=existing_id, coalesced=True)

    await db.commit()
    return JobRef(run_id=str(run_id), coalesced=False)


@router.get("/jobs", response_model=JobsListResponse)
async def list_jobs(
    _session: Annotated[Any, Depends(_require_session)],
    db: AsyncSession = Depends(get_db),
    kind: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> JobsListResponse:
    """Return job run history, newest first (cursor pagination)."""
    limit = min(limit, 100)

    kind_filter = ""
    params: dict[str, Any] = {"limit": limit}

    # Map frontend kind name to DB kind if provided.
    if kind is not None:
        kind_db = _FE_TO_DB.get(kind, kind)
        kind_filter = "WHERE kind = :kind"
        params["kind"] = kind_db

    rows_result = await db.execute(
        sa.text(
            f"""
            SELECT id, kind, status, retry_count, error,
                   claimed_at, completed_at, created_at
            FROM job_run {kind_filter}
            ORDER BY created_at DESC
            LIMIT :limit
            """  # noqa: S608
        ),
        params,
    )
    items = [_map_job_row(row) for row in rows_result.fetchall()]
    now = datetime.now(tz=UTC)
    return JobsListResponse(
        items=items,
        next_cursor=None,
        request_id=str(uuid.uuid4()),
        generated_at=now.isoformat(),
    )


@router.get("/jobs/{job_id}", response_model=JobRunResponse)
async def get_job(
    job_id: uuid.UUID,
    _session: Annotated[Any, Depends(_require_session)],
    db: AsyncSession = Depends(get_db),
) -> JobRunResponse:
    """Return a single job run by ID, or 404."""
    result = await db.execute(
        sa.text(
            """
            SELECT id, kind, status, retry_count, error,
                   claimed_at, completed_at, created_at
            FROM job_run WHERE id = :id
            """
        ),
        {"id": job_id},
    )
    row = result.first()
    if row is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="job not found",
        )
    return _map_job_row(row)


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: uuid.UUID,
    _session: Annotated[Any, Depends(_require_csrf)],
    db: AsyncSession = Depends(get_db),
) -> dict[str, bool]:
    """Cancel a pending or in-progress job.

    Returns 404 if the job does not exist or is already in a terminal state.
    """
    result = await db.execute(
        sa.text(
            """
            UPDATE job_run
               SET status = 'cancelled'
             WHERE id = :id
               AND status IN ('pending', 'in_progress')
            RETURNING id
            """
        ),
        {"id": job_id},
    )
    if result.first() is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="job not found or already in a terminal state",
        )
    await db.commit()
    return {"cancelled": True}


# ---------------------------------------------------------------------------
# Routes — operational status (frontend contract)
# ---------------------------------------------------------------------------


@router.get("/status", response_model=StatusResponse)
async def get_status(
    _session: Annotated[Any, Depends(_require_session)],
    db: AsyncSession = Depends(get_db),
) -> StatusResponse:
    """Return system operational status in the frontend-expected shape."""
    # DB: if we got here the DB is reachable.
    db_status = DbStatus(status="ok")

    # Worker: query worker_status for the most recent heartbeat.
    worker_row = (
        await db.execute(
            sa.text(
                "SELECT status, last_heartbeat_at FROM worker_status "
                "ORDER BY last_heartbeat_at DESC LIMIT 1"
            )
        )
    ).first()
    if worker_row:
        ws_status: str = worker_row[0]
        ws_heartbeat: datetime | None = worker_row[1]
        # Treat a stale heartbeat as stopped.
        stale = (
            ws_heartbeat is None
            or datetime.now(tz=UTC) - ws_heartbeat > timedelta(minutes=_WORKER_STALE_MINUTES)
        )
        if stale:
            fe_status = "stopped"
        elif ws_status in ("idle", "running"):
            fe_status = "running"
        else:
            fe_status = ws_status
        worker_status = WorkerStatus(
            status=fe_status,
            last_heartbeat_at=ws_heartbeat.isoformat() if ws_heartbeat else None,
        )
    else:
        worker_status = WorkerStatus(status="unknown", last_heartbeat_at=None)

    # Recovery: not yet implemented — always inactive.
    recovery = RecoveryStatus(active=False, reason=None)

    # Schedules: same structure as GET /settings.
    settings = await _query_settings_response(db)

    return StatusResponse(
        db=db_status,
        worker=worker_status,
        recovery=recovery,
        schedules=settings.schedules,
        version=None,
    )


# ---------------------------------------------------------------------------
# Export routes — GET /api/v1/exports/portfolio  GET /api/v1/exports/history
# ---------------------------------------------------------------------------


@router.get("/exports/portfolio")
async def export_portfolio(
    _session: Annotated[Any, Depends(_require_session)],
    db: AsyncSession = Depends(get_db),
    format: str = Query(default="json", pattern="^(json|csv)$"),
) -> StreamingResponse:
    """Stream current portfolio as JSON or CSV."""
    import json as _json

    if format == "csv":
        content = await render_portfolio_csv(db)
        return StreamingResponse(
            iter([content]),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="portfolio.csv"'},
        )

    data = await export_current_portfolio(db)
    payload = _json.dumps(data, default=str)
    return StreamingResponse(
        iter([payload]),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="portfolio.json"'},
    )


@router.get("/exports/history")
async def export_history(
    _session: Annotated[Any, Depends(_require_session)],
    db: AsyncSession = Depends(get_db),
    format: str = Query(default="json", pattern="^(json|csv)$"),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
) -> StreamingResponse:
    """Stream full history as JSON or CSV."""
    import json as _json

    if format == "csv":
        content = await render_history_csv(db)
        return StreamingResponse(
            iter([content]),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="history.csv"'},
        )

    data = await export_full_history(db)
    payload = _json.dumps(data, default=str)
    return StreamingResponse(
        iter([payload]),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="history.json"'},
    )


# ---------------------------------------------------------------------------
# Purge routes — GET /api/v1/data/provider-purge-preview
#                POST /api/v1/data/provider-purge
# ---------------------------------------------------------------------------


class PurgePreviewResponse(BaseModel):
    provider: str
    quote_observation_count: int
    quote_set_count: int
    affected_valuation_count: int


class PurgeInput(BaseModel):
    provider: str
    confirm: bool
    current_password: str


class PurgeResult(BaseModel):
    run_id: str
    coalesced: bool


@router.get("/data/provider-purge-preview", response_model=PurgePreviewResponse)
async def get_purge_preview(
    _session: Annotated[Any, Depends(_require_session)],
    db: AsyncSession = Depends(get_db),
    provider: str = Query(...),
) -> PurgePreviewResponse:
    """Return counts of what WOULD be deleted by a purge without modifying data."""
    raw = await preview_purge(db, kind=provider)
    return PurgePreviewResponse(
        provider=provider,
        quote_observation_count=raw["quote_observation_count"],
        quote_set_count=raw["integration_count"],
        affected_valuation_count=raw["valuation_line_count"],
    )


@router.post("/data/provider-purge", response_model=PurgeResult)
async def post_provider_purge(
    body: PurgeInput,
    _session: Annotated[Any, Depends(_require_csrf)],
    db: AsyncSession = Depends(get_db),
) -> PurgeResult:
    """Execute a password-protected provider-data purge."""
    if not body.confirm:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="confirm must be true",
        )
    try:
        await execute_purge(db, kind=body.provider, password=body.current_password)
    except AuthenticationError as exc:
        raise HTTPException(status_code=http_status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    await db.commit()
    return PurgeResult(run_id=str(uuid.uuid4()), coalesced=False)
