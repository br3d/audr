"""FastAPI routes for on-chain events (AUD-307).

GET /api/v1/events
  Returns a paginated list of indexed ERC-20 Transfer events.
  Filters: wallet_id, event_type, token_address.
  Ordered by block_number DESC, log_index DESC.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from audr.api.auth import _require_session
from audr.auth.models import Session
from audr.db import get_db

router = APIRouter(prefix="/api/v1")

_MAX_LIMIT = 200
_DEFAULT_LIMIT = 50


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class OnchainEventResponse(BaseModel):
    id: str
    wallet_id: str
    tx_hash: str
    block_number: int
    log_index: int
    # 'transfer_in' | 'transfer_out'
    event_type: str
    token_address: str
    from_address: str
    to_address: str
    # Raw uint256 as decimal string — never float
    raw_amount: str
    indexed_at: str


class EventsResponse(BaseModel):
    total: int
    limit: int
    offset: int
    events: list[OnchainEventResponse]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/events", response_model=EventsResponse)
async def get_events(
    _session: Annotated[Session, Depends(_require_session)],
    db: AsyncSession = Depends(get_db),
    wallet_id: str | None = Query(default=None),
    event_type: Literal["transfer_in", "transfer_out"] | None = Query(default=None),
    token_address: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> EventsResponse:
    """Return a paginated list of indexed on-chain Transfer events.

    Results are ordered newest-first (block_number DESC, log_index DESC).
    All monetary amounts are raw uint256 decimal strings.
    """
    wallet_uuid: uuid.UUID | None = None
    if wallet_id is not None:
        try:
            wallet_uuid = uuid.UUID(wallet_id)
        except ValueError:
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail="wallet_id is not a valid UUID",
            )

    conditions = []
    params: dict = {"limit": limit, "offset": offset}

    if wallet_uuid is not None:
        conditions.append("wallet_id = :wallet_id")
        params["wallet_id"] = str(wallet_uuid)
    if event_type is not None:
        conditions.append("event_type = :event_type")
        params["event_type"] = event_type
    if token_address is not None:
        conditions.append("token_address = :token_address")
        params["token_address"] = token_address.lower()

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    import sqlalchemy as sa

    count_row = await db.execute(
        sa.text(f"SELECT COUNT(*) FROM onchain_event {where}"),
        params,
    )
    total = count_row.scalar_one()

    rows = await db.execute(
        sa.text(
            f"""
            SELECT id, wallet_id, tx_hash, block_number, log_index,
                   event_type, token_address, from_address, to_address,
                   raw_amount, indexed_at
            FROM onchain_event
            {where}
            ORDER BY block_number DESC, log_index DESC
            LIMIT :limit OFFSET :offset
            """
        ),
        params,
    )

    events = [
        OnchainEventResponse(
            id=str(row[0]),
            wallet_id=str(row[1]),
            tx_hash=str(row[2]),
            block_number=int(row[3]),
            log_index=int(row[4]),
            event_type=str(row[5]),
            token_address=str(row[6]),
            from_address=str(row[7]),
            to_address=str(row[8]),
            raw_amount=str(row[9]),
            indexed_at=row[10].isoformat(),
        )
        for row in rows.fetchall()
    ]

    return EventsResponse(
        total=total,
        limit=limit,
        offset=offset,
        events=events,
    )
