"""FastAPI routes for portfolio history (T071 / US3 / AUD-84).

GET /api/v1/history
  Returns a paginated list of history entries for a given time period.
  Includes gap markers where the timeline has discontinuities.
  No PnL terminology — values are labelled total_value_usd.

GET /api/v1/history/{snapshot_id}
  Returns the constituent holdings lines for a specific snapshot, each
  annotated with its balance_observation_id for exact provenance.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from audr.api.auth import _require_csrf, _require_session
from audr.auth.models import Session
from audr.db import get_db
from audr.portfolio.history_query import (
    MAX_POINTS,
    Period,
    SnapshotDetail,
    get_snapshot_detail,
    query_history,
)

router = APIRouter(prefix="/api/v1")


# ---------------------------------------------------------------------------
# Response schemas — no PnL terminology
# ---------------------------------------------------------------------------


class HistoryEntryResponse(BaseModel):
    snapshot_id: str | None  # None for gap markers
    snapshotted_at: str
    # Decimal string; null when quality is stale/unknown.
    total_value_usd: str | None = None
    quality: str
    included_wallet_count: int
    included_asset_count: int
    has_gap: bool
    is_canonical: bool
    is_gap_marker: bool = False


class HistoryResponse(BaseModel):
    period: str
    items: list[HistoryEntryResponse]
    # Opaque cursor: pass back to get the next page; null = no more pages.
    next_cursor: str | None = None
    request_id: str
    generated_at: str


class SnapshotLineResponse(BaseModel):
    wallet_id: str
    asset_id: str
    token_address: str
    symbol: str
    # Raw uint256 as decimal string.
    raw_amount: str
    block_number: int
    price_usd: str | None = None
    value_usd: str | None = None
    # UUID of the balance_observation used; null for snapshots created before 006.
    observation_id: str | None = None


class SnapshotDetailResponse(BaseModel):
    snapshot_id: str
    snapshotted_at: str
    quality: str
    lines: list[SnapshotLineResponse]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/history", response_model=HistoryResponse)
async def get_history(
    _session: Annotated[Session, Depends(_require_session)],
    db: AsyncSession = Depends(get_db),
    period: Literal["24h", "7d", "30d", "90d", "1y", "all"] = Query(default="7d"),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=MAX_POINTS, ge=1, le=MAX_POINTS),
) -> HistoryResponse:
    """Return portfolio value history for the requested period.

    Entries are ordered newest-first.  Gap markers (is_gap_marker=true) are
    synthetic entries inserted where the timeline has a discontinuity; they
    have no snapshot_id and total_value_usd is null.

    Use next_cursor to fetch the next page (null = exhausted).
    """
    cursor_uuid: uuid.UUID | None = None
    if cursor is not None:
        try:
            cursor_uuid = uuid.UUID(cursor)
        except ValueError:
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail="cursor is not a valid UUID",
            )

    page = await query_history(
        db,
        period=period,  # type: ignore[arg-type]
        cursor=cursor_uuid,
        limit=limit,
    )

    items = [
        HistoryEntryResponse(
            snapshot_id=str(e.snapshot_id) if not e.is_gap_marker else None,
            snapshotted_at=e.snapshotted_at.isoformat(),
            total_value_usd=(_fmt(e.total_value_usd) if e.total_value_usd is not None else None),
            quality=e.quality,
            included_wallet_count=e.included_wallet_count,
            included_asset_count=e.included_asset_count,
            has_gap=e.has_gap,
            is_canonical=e.is_canonical,
            is_gap_marker=e.is_gap_marker,
        )
        for e in page.entries
    ]

    return HistoryResponse(
        period=period,
        items=items,
        next_cursor=str(page.next_cursor) if page.next_cursor is not None else None,
        request_id=str(uuid.uuid4()),
        generated_at=datetime.now(tz=UTC).isoformat(),
    )


@router.get(
    "/history/{snapshot_id}",
    response_model=SnapshotDetailResponse,
)
async def get_history_snapshot(
    snapshot_id: uuid.UUID,
    _session: Annotated[Session, Depends(_require_session)],
    db: AsyncSession = Depends(get_db),
) -> SnapshotDetailResponse:
    """Return constituent holdings lines for a specific historical snapshot.

    Each line includes observation_id for exact balance provenance.  Lines are
    ordered by value_usd descending (unknown values last).
    """
    detail: SnapshotDetail | None = await get_snapshot_detail(db, snapshot_id=snapshot_id)
    if detail is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Snapshot not found",
        )

    return SnapshotDetailResponse(
        snapshot_id=str(detail.snapshot_id),
        snapshotted_at=detail.snapshotted_at.isoformat(),
        quality=detail.quality,
        lines=[
            SnapshotLineResponse(
                wallet_id=str(line.wallet_id),
                asset_id=str(line.asset_id),
                token_address=line.token_address,
                symbol=line.symbol,
                raw_amount=line.raw_amount,
                block_number=line.block_number,
                price_usd=line.price_usd,
                value_usd=line.value_usd,
                observation_id=(
                    str(line.observation_id) if line.observation_id is not None else None
                ),
            )
            for line in detail.lines
        ],
    )


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _fmt(v: Decimal) -> str:
    return f"{v:.18f}".rstrip("0").rstrip(".")
