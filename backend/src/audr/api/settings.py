"""Authenticated settings, job list/detail/cancel, and operational status routes (T083 / US4)."""

from __future__ import annotations

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
from audr.assets.catalog import get_catalog_status
from audr.auth.models import Session
from audr.auth.service import AuthenticationError
from audr.db import get_db
from audr.jobs.quotes import get_quote_status
from audr.jobs.store import JobKind, enqueue_job, get_worker_heartbeat
from audr.operations.exports import (
    export_current_portfolio,
    export_full_history,
    render_portfolio_csv,
    stream_history_csv,
)
from audr.operations.purge import execute_purge, preview_purge
from audr.settings.schedules import (
    RevisionConflictError,
    get_schedule,
    pause_schedule,
    resume_schedule,
    update_schedule,
)
from audr.wallets.service import get_wallet

router = APIRouter(prefix="/api/v1")

# ---------------------------------------------------------------------------
# Kind mapping: DB internal names ↔ frontend API names
# ---------------------------------------------------------------------------

_DB_TO_FE: dict[str, str] = {
    "balance_scan": "balances",
    "quote_refresh": "quotes",
    "discovery": "discovery",
    "valuation": "valuation",
}
_FE_TO_DB: dict[str, str] = {v: k for k, v in _DB_TO_FE.items()}

# Kinds that may be scoped to a single wallet (AUD-399/AUD-400) — the
# per-wallet "Refresh balances" / "Discover tokens" actions on the wallet
# card. Any other kind rejects a wallet_id with 422.
_WALLET_SCOPED_KINDS: frozenset[str] = frozenset({"balances", "discovery"})


# ---------------------------------------------------------------------------
# Pydantic schemas — internal (per-kind schedule CRUD)
# ---------------------------------------------------------------------------


class ScheduleRead(BaseModel):
    kind: str
    enabled: bool
    paused: bool
    revision: int
    freshness_s: int | None
    budget_calls_per_day: int | None
    last_run_at: datetime | None
    next_run_at: datetime | None


class ScheduleUpdate(BaseModel):
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


def _validate_schedule_patch(patch: SettingsSchedulePatch) -> None:
    """Raise HTTP 422 when interval_seconds or freshness_seconds are non-positive.

    ``freshness_s`` (stored directly, no cron conversion) drives ``is_due``:
    a value of 0 or less makes the schedule due on every check.
    """
    if patch.interval_seconds is not None and patch.interval_seconds <= 0:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="interval_seconds must be positive",
        )
    if patch.freshness_seconds is not None and patch.freshness_seconds <= 0:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="freshness_seconds must be positive",
        )


class DbStatus(BaseModel):
    status: str


class WorkerStatus(BaseModel):
    status: str
    last_heartbeat_at: str | None = None


class RecoveryStatus(BaseModel):
    active: bool
    reason: str | None = None


class CatalogStatusModel(BaseModel):
    status: str
    entry_count: int


class QuoteStatusModel(BaseModel):
    status: str
    provider: str
    unpriced_count: int


class StatusResponse(BaseModel):
    db: DbStatus
    worker: WorkerStatus
    recovery: RecoveryStatus
    catalog: CatalogStatusModel
    quotes: QuoteStatusModel
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
    wallet_id: uuid.UUID | None = None


# ---------------------------------------------------------------------------
# Internal helpers — kept for per-kind schedule CRUD routes
# ---------------------------------------------------------------------------

_SELECT_SCHEDULE_COLS = """
    id, kind, enabled, revision, paused_at,
    freshness_s, budget_calls_per_day, last_run_at, next_run_at, updated_at
"""


# ANN401: row is a positional SQLAlchemy Row, indexed by column position below;
# there is no narrower static type for an ad-hoc `sa.text()` result.
def _row_to_schedule_read(row: Any) -> ScheduleRead:  # noqa: ANN401 — row is a positional SQLAlchemy Row, indexed by column position; no narrower static type for an ad-hoc sa.text() result
    return ScheduleRead(
        kind=row[1],
        enabled=row[2],
        paused=row[4] is not None,
        revision=row[3],
        freshness_s=row[5],
        budget_calls_per_day=row[6],
        last_run_at=row[7],
        next_run_at=row[8],
    )


def _dict_to_schedule_read(d: dict) -> ScheduleRead:  # type: ignore[type-arg]
    return ScheduleRead(
        kind=d["kind"],
        enabled=d["enabled"],
        paused=d["paused_at"] is not None,
        revision=d["revision"],
        freshness_s=d["freshness_s"],
        budget_calls_per_day=d["budget_calls_per_day"],
        last_run_at=d["last_run_at"],
        next_run_at=d["next_run_at"],
    )


def _next_due_at(
    *,
    enabled: bool,
    paused_at: datetime | None,
    freshness_s: int | None,
    last_run_at: datetime | None,
    next_run_at: datetime | None,
) -> str | None:
    """Return the ISO timestamp of the next scheduled run, or None.

    ``schedule.next_run_at`` is never written by the dispatcher — ``is_due()``
    derives readiness from ``last_run_at + freshness_s`` instead (AUD-456), so
    reading the column alone always yielded NULL and the Status page showed a
    blank "Next scheduled runs" card. Mirror the dispatcher's arithmetic here,
    preferring an explicit ``next_run_at`` if one is ever persisted.
    """
    if not enabled or paused_at is not None:
        return None
    if next_run_at is not None:
        return next_run_at.isoformat()
    if freshness_s is None:
        # No freshness window — is_due() fires on the next dispatcher tick.
        return datetime.now(UTC).isoformat()
    if last_run_at is None:
        return datetime.now(UTC).isoformat()
    return (last_run_at + timedelta(seconds=freshness_s)).isoformat()


# ANN401: row is a positional SQLAlchemy Row, indexed by column position below;
# there is no narrower static type for an ad-hoc `sa.text()` result.
def _row_to_schedule_config(row: Any) -> tuple[str, ScheduleConfig]:  # noqa: ANN401 — row is a positional SQLAlchemy Row, indexed by column position; no narrower static type for an ad-hoc sa.text() result
    """Return (frontend_kind, ScheduleConfig) from a schedule table row."""
    kind_db: str = row[1]
    enabled: bool = row[2]
    paused_at: datetime | None = row[4]
    freshness_s: int | None = row[5]
    last_run_at: datetime | None = row[7]
    next_run_at: datetime | None = row[8]

    kind_fe = _DB_TO_FE.get(kind_db, kind_db)
    next_due = _next_due_at(
        enabled=enabled,
        paused_at=paused_at,
        freshness_s=freshness_s,
        last_run_at=last_run_at,
        next_run_at=next_run_at,
    )

    return kind_fe, ScheduleConfig(
        enabled=enabled,
        interval_seconds=freshness_s or 0,
        freshness_seconds=freshness_s,
        next_due_at=next_due,
    )


# ANN401: row is a positional SQLAlchemy Row, unpacked by position below;
# there is no narrower static type for an ad-hoc `sa.text()` result.
def _map_job_row(row: Any) -> JobRunResponse:  # noqa: ANN401 — row is a positional SQLAlchemy Row, unpacked by position; no narrower static type for an ad-hoc sa.text() result
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
        # _SELECT_SCHEDULE_COLS is a module-level constant; nothing request-derived is interpolated.
        sa.text(f"SELECT {_SELECT_SCHEDULE_COLS} FROM schedule ORDER BY kind")  # noqa: S608 — _SELECT_SCHEDULE_COLS is a module-level constant; nothing request-derived is interpolated
    )
    rows = result.fetchall()

    schedules: dict[str, ScheduleConfig] = {}
    max_revision = 0
    for row in rows:
        revision: int = row[3]
        kind_fe, config = _row_to_schedule_config(row)
        schedules[kind_fe] = config
        max_revision = max(max_revision, revision)

    return SettingsResponse(revision=str(max_revision), schedules=schedules)


# ---------------------------------------------------------------------------
# Routes — batch settings (frontend contract)
# ---------------------------------------------------------------------------


@router.get("/settings", response_model=SettingsResponse)
async def get_settings(
    _session: Annotated[Session, Depends(_require_session)],
    db: AsyncSession = Depends(get_db),
) -> SettingsResponse:
    """Return all schedule settings in the frontend-expected shape."""
    return await _query_settings_response(db)


@router.patch("/settings", response_model=SettingsResponse)
async def patch_settings(
    body: SettingsPatch,
    _session: Annotated[Session, Depends(_require_csrf)],
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
            _validate_schedule_patch(patch)
            kind_db = _FE_TO_DB.get(kind_fe, kind_fe)
            # ``interval_seconds`` and ``freshness_seconds`` are two names for the
            # same ``schedule.freshness_s`` column, and GET /settings echoes the
            # value back under both. ``interval_seconds`` is the field the
            # Schedules page actually edits, so it must win when both arrive —
            # preferring ``freshness_seconds`` meant a client that round-tripped
            # the GET payload silently rewrote the old value over the new one
            # and the change appeared not to save at all (AUD-397).
            freshness_s = (
                patch.interval_seconds
                if patch.interval_seconds is not None
                else patch.freshness_seconds
            )

            sets: list[str] = ["revision = revision + 1"]
            params: dict[str, Any] = {"kind": kind_db}

            if patch.enabled is not None:
                sets.append("enabled = :enabled")
                params["enabled"] = patch.enabled
            if freshness_s is not None:
                sets.append("freshness_s = :freshness_s")
                params["freshness_s"] = freshness_s

            if len(sets) > 1:  # more than just the revision bump
                await db.execute(
                    sa.text(
                        # sets is a fixed ":param" vocabulary; values are bound, never interpolated.
                        "UPDATE schedule SET "  # noqa: S608 — sets is a fixed ":param" vocabulary; values are bound, never interpolated
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
    _session: Annotated[Session, Depends(_require_session)],
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
    _session: Annotated[Session, Depends(_require_csrf)],
    db: AsyncSession = Depends(get_db),
) -> ScheduleRead:
    """Update configurable schedule fields."""
    try:
        d = await update_schedule(
            db,
            kind=kind,
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
    _session: Annotated[Session, Depends(_require_csrf)],
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
    _session: Annotated[Session, Depends(_require_csrf)],
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


@router.post("/jobs", response_model=JobRef, status_code=http_status.HTTP_202_ACCEPTED)
async def trigger_job(
    body: TriggerJobInput,
    _session: Annotated[Session, Depends(_require_csrf)],
    db: AsyncSession = Depends(get_db),
) -> JobRef:
    """Queue an immediate job run, coalescing if one is already queued or active.

    Enqueues a ``pending`` row (AUD-313/AUD-318 pattern) instead of inserting an
    ``in_progress`` row directly — a run only starts once the worker actually
    claims and dispatches it via :func:`claim_pending_job`, so the ID returned
    here is always a real, eventually-executed run rather than a zombie the
    worker will never pick up.

    ``wallet_id`` scopes ``balances``/``discovery`` requests to a single wallet
    (AUD-399/AUD-400) — the wallet-card "Refresh balances" / "Discover tokens"
    actions, so a single address can be refreshed without re-scanning every
    tracked wallet. Coalescing is scope-aware: a global request never swallows
    a per-wallet one and vice versa, and two requests for different wallets
    never coalesce onto each other.
    """
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

    wallet_id_str: str | None = None
    if body.wallet_id is not None:
        if body.kind not in _WALLET_SCOPED_KINDS:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"wallet_id is not supported for kind: {body.kind!r}",
            )
        wallet = await get_wallet(db, wallet_id=body.wallet_id)
        if wallet is None:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail="wallet not found",
            )
        wallet_id_str = str(body.wallet_id)

    # Coalesce if a run of the same kind AND the same scope is already queued
    # or running — the worker will pick up the existing request; no need to
    # stack another. A global run (params IS NULL) and a per-wallet run never
    # match each other, and two different wallet ids never match each other.
    existing = await db.execute(
        sa.text(
            "SELECT id FROM job_run WHERE kind = :kind"
            " AND status IN ('pending', 'in_progress')"
            " AND ("
            "   (CAST(:wallet_id AS text) IS NULL AND params IS NULL)"
            "   OR (params ->> 'wallet_id' = CAST(:wallet_id AS text))"
            " )"
            " ORDER BY created_at DESC LIMIT 1"
        ),
        {"kind": kind_db, "wallet_id": wallet_id_str},
    )
    existing_row = existing.first()
    if existing_row is not None:
        return JobRef(run_id=str(existing_row[0]), coalesced=True)

    params = {"wallet_id": wallet_id_str} if wallet_id_str is not None else None
    run_id = await enqueue_job(db, kind=job_kind, params=params)
    await db.commit()
    return JobRef(run_id=str(run_id), coalesced=False)


@router.get("/jobs", response_model=JobsListResponse)
async def list_jobs(
    _session: Annotated[Session, Depends(_require_session)],
    db: AsyncSession = Depends(get_db),
    kind: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> JobsListResponse:
    """Return job run history, newest first (keyset cursor pagination)."""
    limit = min(limit, 100)

    conditions: list[str] = []
    params: dict[str, Any] = {"limit": limit + 1}

    # Map frontend kind name to DB kind if provided.
    if kind is not None:
        kind_db = _FE_TO_DB.get(kind, kind)
        conditions.append("kind = :kind")
        params["kind"] = kind_db

    if cursor is not None:
        try:
            cursor_uuid = uuid.UUID(cursor)
        except ValueError as exc:
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail="cursor is not a valid UUID",
            ) from exc
        # Keyset pagination: strictly-older-than the cursor row, tie-broken by
        # id since bulk-enqueued jobs can share the same created_at value.
        conditions.append(
            "(created_at, id) < (SELECT created_at, id FROM job_run WHERE id = :cursor)"
        )
        params["cursor"] = cursor_uuid

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    rows_result = await db.execute(
        sa.text(
            # where_clause is a fixed ":param" vocabulary; values are bound, never interpolated.
            f"""
            SELECT id, kind, status, retry_count, error,
                   claimed_at, completed_at, created_at
            FROM job_run {where_clause}
            ORDER BY created_at DESC, id DESC
            LIMIT :limit
            """  # noqa: S608 — where_clause is a fixed ":param" vocabulary; values are bound, never interpolated
        ),
        params,
    )
    rows = rows_result.fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [_map_job_row(row) for row in rows]
    next_cursor = str(rows[-1][0]) if has_more and rows else None
    now = datetime.now(tz=UTC)
    return JobsListResponse(
        items=items,
        next_cursor=next_cursor,
        request_id=str(uuid.uuid4()),
        generated_at=now.isoformat(),
    )


@router.get("/jobs/{job_id}", response_model=JobRunResponse)
async def get_job(
    job_id: uuid.UUID,
    _session: Annotated[Session, Depends(_require_session)],
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


@router.post("/jobs/{job_id}/cancel", status_code=http_status.HTTP_202_ACCEPTED)
async def cancel_job(
    job_id: uuid.UUID,
    _session: Annotated[Session, Depends(_require_csrf)],
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
    _session: Annotated[Session, Depends(_require_session)],
    db: AsyncSession = Depends(get_db),
) -> StatusResponse:
    """Return system operational status in the frontend-expected shape."""
    # DB: if we got here the DB is reachable.
    db_status = DbStatus(status="ok")

    # Worker: query worker_status for the most recent heartbeat.
    ws_status, ws_heartbeat = await get_worker_heartbeat(db)
    worker_status = WorkerStatus(
        status=ws_status,
        last_heartbeat_at=ws_heartbeat.isoformat() if ws_heartbeat else None,
    )

    # Recovery: not yet implemented — always inactive.
    recovery = RecoveryStatus(active=False, reason=None)

    # Catalog: DEGRADED means ERC-20 discovery finds zero candidates (AUD-357) —
    # must stay visible here rather than only as a worker-log WARNING.
    catalog = await get_catalog_status(db)

    # Quotes: which provider is pricing holdings, and whether anything is
    # still unpriced (AUD-358) — same visibility rationale as catalog above.
    quotes = await get_quote_status(db)

    # Schedules: same structure as GET /settings.
    settings = await _query_settings_response(db)

    return StatusResponse(
        db=db_status,
        worker=worker_status,
        recovery=recovery,
        catalog=CatalogStatusModel(status=catalog.status.value, entry_count=catalog.entry_count),
        quotes=QuoteStatusModel(
            status=quotes.status.value,
            provider=quotes.provider,
            unpriced_count=quotes.unpriced_count,
        ),
        schedules=settings.schedules,
        version=None,
    )


# ---------------------------------------------------------------------------
# Export routes — GET /api/v1/exports/portfolio  GET /api/v1/exports/history
# ---------------------------------------------------------------------------


@router.get("/exports/portfolio")
async def export_portfolio(
    _session: Annotated[Session, Depends(_require_session)],
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


def _parse_export_date(value: str | None, param: str) -> datetime | None:
    """Parse an ISO-8601 date/datetime query param; raise 422 on bad input."""
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid '{param}' parameter: {exc}",
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


@router.get("/exports/history")
async def export_history(
    _session: Annotated[Session, Depends(_require_session)],
    db: AsyncSession = Depends(get_db),
    format: str = Query(default="json", pattern="^(json|csv)$"),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
) -> StreamingResponse:
    """Stream full history as JSON or CSV.  Optional ``from`` / ``to`` bound the date range."""
    import json as _json

    from_dt = _parse_export_date(from_, "from")
    to_dt = _parse_export_date(to, "to")

    if format == "csv":
        return StreamingResponse(
            stream_history_csv(db, from_=from_dt, to_=to_dt),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="history.csv"'},
        )

    data = await export_full_history(db, from_=from_dt, to_=to_dt)
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
    purged: bool
    quote_observations_deleted: int
    valuation_lines_deleted: int


@router.get("/data/provider-purge-preview", response_model=PurgePreviewResponse)
async def get_purge_preview(
    _session: Annotated[Session, Depends(_require_session)],
    db: AsyncSession = Depends(get_db),
    provider: str = Query(...),
) -> PurgePreviewResponse:
    """Return counts of what WOULD be deleted by a purge without modifying data."""
    raw = await preview_purge(db, kind=provider)
    return PurgePreviewResponse(
        provider=provider,
        quote_observation_count=raw["quote_observation_count"],
        quote_set_count=raw["quote_set_count"],
        affected_valuation_count=raw["valuation_line_count"],
    )


@router.post("/data/provider-purge", response_model=PurgeResult)
async def post_provider_purge(
    body: PurgeInput,
    _session: Annotated[Session, Depends(_require_csrf)],
    db: AsyncSession = Depends(get_db),
) -> PurgeResult:
    """Execute a password-protected provider-data purge."""
    if not body.confirm:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="confirm must be true",
        )
    try:
        result = await execute_purge(db, kind=body.provider, password=body.current_password)
    except AuthenticationError as exc:
        raise HTTPException(status_code=http_status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    await db.commit()
    return PurgeResult(
        purged=result["purged"],
        quote_observations_deleted=result["quote_observations_deleted"],
        valuation_lines_deleted=result["valuation_lines_deleted"],
    )
