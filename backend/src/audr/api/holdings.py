"""FastAPI routes for holdings / balance aggregation (T042 / US1)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from audr.api.auth import _require_csrf
from audr.auth.models import Session
from audr.db import get_db
from audr.portfolio.balances import get_holdings

router = APIRouter(prefix="/api/v1")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class HoldingResponse(BaseModel):
    wallet_address: str
    token_address: str
    raw_amount: int | None
    block_number: int | None


class WalletHoldingsResponse(BaseModel):
    wallet_address: str
    holdings: list[HoldingResponse]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/wallets/{wallet_id}/holdings", response_model=WalletHoldingsResponse)
async def get_wallet_holdings(
    wallet_id: uuid.UUID,
    _session: Annotated[Session, Depends(_require_csrf)],
    db: AsyncSession = Depends(get_db),
) -> WalletHoldingsResponse:
    # Look up wallet address by ID.
    from sqlalchemy import select

    from audr.wallets.models import Wallet

    result = await db.execute(select(Wallet.address).where(Wallet.id == wallet_id))
    row = result.first()
    if row is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Wallet not found"
        )
    address = row[0]

    observations = await get_holdings(db, wallet_address=address)
    return WalletHoldingsResponse(
        wallet_address=address,
        holdings=[
            HoldingResponse(
                wallet_address=obs.wallet_address,
                token_address=obs.token_address,
                raw_amount=obs.raw_amount,
                block_number=obs.block_number,
            )
            for obs in observations
        ],
    )
