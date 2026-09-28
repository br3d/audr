"""Authenticated settings, job list/detail/cancel, and operational status routes (T083 / US4)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from audr.auth.dependencies import require_session
from audr.db import get_db
from audr.operations.exports import export_current_portfolio, export_full_history, render_portfolio_csv
from audr.operations.migrations import check_migration_readiness
from audr.settings.schedules import (
    RevisionConflictError,
    get_schedule,
    pause_schedule,
    resume_schedule,
    update_schedule,
)

router = APIRouter(prefix="/api/v1")


# ---------------------------------------------------------------------------
# Pydantic schemas
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


class JobRunRead(BaseModel):
    id: uuid.UUID
    kind: str
    status: str
    retry_count: int
    error: str | None
    claimed_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


class JobListResponse(BaseModel):
    items: list[JobRunRead]
    total: int
    page: int
    page_size: int


class OperationalStatus(BaseModel):
    migration_up_to_date: bool
    key_initialized: bool
    active_job_count: int
    schedules: list[ScheduleRead]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_SELECT_SCHEDULE_COLS = """
    id, kind, cron_expr, enabled, revision, paused_at,
    freshness_s, budget_calls_per_day, last_run_at, next_run_at, updated_at
"""


def _row_to_schedule_read(row: Any) -> ScheduleRead:
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


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/settings", response_model=list[ScheduleRead])
async def list_schedules(
    _session: Annotated[Any, Depends(require_session)],
    db: AsyncSession = Depends(get_db),
) -> list[ScheduleRead]:
    """Return all schedule rows."""
    result = await db.execute(
        sa.text(
            f"SELECT {_SELECT_SCHEDULE_COLS} FROM schedule ORDER BY kind"  # noqa: S608
        )
    )
    return [_row_to_schedule_read(row) for row in result.fetchall()]


@router.get("/settings/schedules/{kind}", response_model=ScheduleRead)
async def get_schedule_route(
    kind: str,
    _session: Annotated[Any, Depends(require_session)],
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
    _session: Annotated[Any, Depends(require_session)],
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
    _session: Annotated[Any, Depends(require_session)],
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
    _session: Annotated[Any, Depends(require_session)],
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


@router.get("/jobs", response_model=JobListResponse)
async def list_jobs(
    _session: Annotated[Any, Depends(require_session)],
    db: AsyncSession = Depends(get_db),
    page: int = 1,
    page_size: int = 20,
    kind: str | None = None,
) -> JobListResponse:
    """Return paginated job run history, newest first."""
    page_size = min(page_size, 100)
    offset = (page - 1) * page_size

    kind_filter = "WHERE kind = :kind" if kind is not None else ""
    params: dict[str, Any] = {"limit": page_size, "offset": offset}
    if kind is not None:
        params["kind"] = kind

    count_result = await db.execute(
        sa.text(f"SELECT COUNT(*) FROM job_run {kind_filter}"),  # noqa: S608
        params,
    )
    total: int = count_result.scalar() or 0

    rows_result = await db.execute(
        sa.text(
            f"""
            SELECT id, kind, status, retry_count, error,
                   claimed_at, completed_at, created_at
            FROM job_run {kind_filter}
            ORDER BY created_at DESC
            LIMIT :limit OFFSET :offset
            """  # noqa: S608
        ),
        params,
    )

    items = [
        JobRunRead(
            id=row[0],
            kind=row[1],
            status=row[2],
            retry_count=row[3],
            error=row[4],
            claimed_at=row[5],
            completed_at=row[6],
            created_at=row[7],
        )
        for row in rows_result.fetchall()
    ]

    return JobListResponse(items=items, total=total, page=page, page_size=page_size)


@router.get("/jobs/{job_id}", response_model=JobRunRead)
async def get_job(
    job_id: uuid.UUID,
    _session: Annotated[Any, Depends(require_session)],
    db: AsyncSession = Depends(get_db),
) -> JobRunRead:
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
    return JobRunRead(
        id=row[0],
        kind=row[1],
        status=row[2],
        retry_count=row[3],
        error=row[4],
        claimed_at=row[5],
        completed_at=row[6],
        created_at=row[7],
    )


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: uuid.UUID,
    _session: Annotated[Any, Depends(require_session)],
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


@router.get("/status", response_model=OperationalStatus)
async def get_status(
    _session: Annotated[Any, Depends(require_session)],
    db: AsyncSession = Depends(get_db),
) -> OperationalStatus:
    """Return overall system operational status."""
    migration_info = await check_migration_readiness(db)

    key_result = await db.execute(
        sa.text("SELECT COUNT(*) FROM key_state")
    )
    key_initialized: bool = (key_result.scalar() or 0) > 0

    active_result = await db.execute(
        sa.text(
            "SELECT COUNT(*) FROM job_run WHERE status IN ('pending', 'in_progress')"
        )
    )
    active_job_count: int = active_result.scalar() or 0

    sched_result = await db.execute(
        sa.text(
            f"SELECT {_SELECT_SCHEDULE_COLS} FROM schedule ORDER BY kind"  # noqa: S608
        )
    )
    schedules = [_row_to_schedule_read(row) for row in sched_result.fetchall()]

    return OperationalStatus(
        migration_up_to_date=migration_info["up_to_date"],
        key_initialized=key_initialized,
        active_job_count=active_job_count,
        schedules=schedules,
    )


# ---------------------------------------------------------------------------
# Data export route — POST /api/v1/data/export
# ---------------------------------------------------------------------------


@router.post("/data/export")
async def export_data(
    _session: Annotated[Any, Depends(require_session)],
    db: AsyncSession = Depends(get_db),
    format: str = Query(default="json", pattern="^(json|csv)$"),
    scope: str = Query(default="current", pattern="^(current|history)$"),
) -> StreamingResponse:
    """Stream portfolio data as JSON or CSV.

    Query params:
      - format: "json" (default) or "csv"
      - scope: "current" (default, current portfolio) or "history" (all snapshots)
    """
    import json as _json

    if format == "csv":
        content = await render_portfolio_csv(db)
        return StreamingResponse(
            iter([content]),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="portfolio.csv"'},
        )

    if scope == "history":
        data = await export_full_history(db)
    else:
        data = await export_current_portfolio(db)

    payload = _json.dumps(data, default=str)
    return StreamingResponse(
        iter([payload]),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="portfolio.json"'},
    )
