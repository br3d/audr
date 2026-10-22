"""FastAPI routes for news alerts (ADR-007 phase 1, AUD-583).

GET /api/v1/alerts
  Returns a paginated list of `news_alert` rows, unread first (then newest
  first), with the total unread count for the UI badge. Populated by the
  NEWS_ANALYZE background job (audr.jobs.news_analyze) — this endpoint only
  ever reads it.

POST /api/v1/alerts/{alert_id}/read
  Marks an alert read (idempotent — re-marking an already-read alert is a
  no-op, not an error).
"""

from __future__ import annotations

import uuid
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from audr.api.auth import ReadPrincipal, _require_csrf, _require_read_auth
from audr.auth.models import Session  # still used in CSRF-protected route
from audr.db import get_db

router = APIRouter(prefix="/api/v1")

_MAX_LIMIT = 100
_DEFAULT_LIMIT = 20


class AlertItem(BaseModel):
    id: str
    asset_news_id: str
    asset_id: str
    category: str
    severity: str
    summary: str
    classifier: str
    model: str | None
    created_at: str
    read_at: str | None
    # Pushed at a human, or merely accepted? `dispatched_at` has meant
    # "notify.sh took it" since ADR-007 rev 2.1, and notify.sh accepts an alert
    # no sink would take by spooling it for a later replay — so
    # `dispatched_via = 'spool'` is a row that left the dispatcher's queue
    # without reaching a channel (AUD-595). NULL is either "not dispatched" or
    # "dispatched before the road was recorded"; `dispatched_at` separates them.
    dispatched_at: str | None = None
    dispatched_via: str | None = None
    # What the alert is actually about. `summary` only ever holds the
    # classifier's reason ("heuristic match: 'exploit'"), which repeats across
    # unrelated stories and made distinct alerts read as duplicates in the bell
    # (AUD-683). The symbol and headline are what tell two alerts apart, so the
    # list endpoint joins them in rather than making the client fetch per row.
    # Typed optional and joined with LEFT JOIN so a row whose article or asset
    # went missing still lists (today both FKs are NOT NULL, and the article
    # FK cascades, so in practice these are always populated) — a dropped
    # alert is a worse failure here than an alert missing its headline.
    asset_symbol: str | None = None
    article_title: str | None = None
    article_url: str | None = None


class AlertsResponse(BaseModel):
    total: int
    unread_count: int
    limit: int
    offset: int
    alerts: list[AlertItem]


class AlertReadResponse(BaseModel):
    id: str
    read_at: str


@router.get("/alerts", response_model=AlertsResponse)
async def list_alerts(
    _auth: Annotated[ReadPrincipal, Depends(_require_read_auth)],
    db: AsyncSession = Depends(get_db),
    unread: bool = False,
    limit: int = _DEFAULT_LIMIT,
    offset: int = 0,
) -> AlertsResponse:
    """Return `news_alert` rows, unread first, then newest first."""
    if not 1 <= limit <= _MAX_LIMIT:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=f"limit must be between 1 and {_MAX_LIMIT}",
        )
    if offset < 0:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="offset must be >= 0",
        )

    # Qualified with the `na` alias so the same literal can be reused by the
    # list query, which joins `asset` and `asset_news` alongside it.
    where_clause = "WHERE na.read_at IS NULL" if unread else ""

    total = (
        await db.execute(sa.text(f"SELECT COUNT(*) FROM news_alert na {where_clause}"))  # noqa: S608 -- where_clause is one of two hardcoded literals, not user input
    ).scalar_one()
    unread_count = (
        await db.execute(sa.text("SELECT COUNT(*) FROM news_alert WHERE read_at IS NULL"))
    ).scalar_one()

    rows = await db.execute(
        sa.text(
            f"""
            SELECT na.id, na.asset_news_id, na.asset_id, na.category, na.severity,
                   na.summary, na.classifier, na.model, na.created_at, na.read_at,
                   na.dispatched_at, na.dispatched_via,
                   a.symbol, an.title, an.url
            FROM news_alert na
            LEFT JOIN asset a ON a.id = na.asset_id
            LEFT JOIN asset_news an ON an.id = na.asset_news_id
            {where_clause}
            ORDER BY (na.read_at IS NULL) DESC, na.created_at DESC
            LIMIT :limit OFFSET :offset
            """  # noqa: S608 -- where_clause is one of two hardcoded literals, not user input
        ),
        {"limit": limit, "offset": offset},
    )

    alerts = [
        AlertItem(
            id=str(row[0]),
            asset_news_id=str(row[1]),
            asset_id=str(row[2]),
            category=str(row[3]),
            severity=str(row[4]),
            summary=str(row[5]),
            classifier=str(row[6]),
            model=row[7],
            created_at=row[8].isoformat(),
            read_at=row[9].isoformat() if row[9] is not None else None,
            dispatched_at=row[10].isoformat() if row[10] is not None else None,
            dispatched_via=row[11],
            asset_symbol=row[12],
            article_title=row[13],
            article_url=row[14],
        )
        for row in rows.fetchall()
    ]

    return AlertsResponse(
        total=total,
        unread_count=unread_count,
        limit=limit,
        offset=offset,
        alerts=alerts,
    )


@router.post("/alerts/{alert_id}/read", response_model=AlertReadResponse)
async def mark_alert_read(
    alert_id: str,
    _session: Annotated[Session, Depends(_require_csrf)],
    db: AsyncSession = Depends(get_db),
) -> AlertReadResponse:
    """Mark an alert as read. Idempotent: re-marking is a no-op, not an error."""
    try:
        alert_uuid = uuid.UUID(alert_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="alert_id is not a valid UUID",
        ) from exc

    row = (
        await db.execute(
            sa.text(
                """
                UPDATE news_alert
                SET read_at = COALESCE(read_at, now())
                WHERE id = :id
                RETURNING read_at
                """
            ),
            {"id": str(alert_uuid)},
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="alert not found")
    await db.commit()

    return AlertReadResponse(id=str(alert_uuid), read_at=row[0].isoformat())
