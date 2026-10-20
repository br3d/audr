"""FastAPI routes for holdings / balance aggregation (T042 / T059 / US1 / US2)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from audr.api.auth import ReadPrincipal, TokenContext, _require_read_auth
from audr.db import get_db
from audr.portfolio.balances import get_holdings
from audr.portfolio.money import format_decimal, raw_to_quantity
from audr.portfolio.snapshot import get_latest_snapshot_lines
from audr.wallets.service import get_wallet, list_wallets

router = APIRouter(prefix="/api/v1")


def _redact(address: str, label: str | None) -> str:
    """Replace a wallet address with "label (0x1234…abcd)" for redact_addresses tokens."""
    short = f"{address[:6]}…{address[-4:]}" if len(address) > 10 else address
    return f"{label} ({short})" if label else short


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class HoldingResponse(BaseModel):
    wallet_address: str
    token_address: str
    symbol: str | None = None
    decimals: int | None = None
    # Raw uint256 as decimal string (never float).
    raw_amount: str | None = None
    # Human-readable quantity (raw / 10^decimals), 18 decimal places.
    quantity: str | None = None
    # USD price and value from most recent snapshot; null if unknown.
    price_usd: str | None = None
    value_usd: str | None = None
    block_number: int | None = None


class WalletHoldingsResponse(BaseModel):
    wallet_address: str
    holdings: list[HoldingResponse]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/wallets/{wallet_id}/holdings", response_model=WalletHoldingsResponse)
async def get_wallet_holdings(
    wallet_id: uuid.UUID,
    _auth: Annotated[ReadPrincipal, Depends(_require_read_auth)],
    db: AsyncSession = Depends(get_db),
) -> WalletHoldingsResponse:
    wallet = await get_wallet(db, wallet_id=wallet_id)
    if wallet is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Wallet not found")
    address = wallet.address

    observations = await get_holdings(db, wallet_address=address)
    out_address = (
        _redact(address, wallet.label)
        if isinstance(_auth, TokenContext) and _auth.redact_addresses
        else address
    )
    return WalletHoldingsResponse(
        wallet_address=out_address,
        holdings=[
            HoldingResponse(
                wallet_address=out_address,
                token_address=obs.token_address,
                raw_amount=str(obs.raw_amount) if obs.raw_amount is not None else None,
                block_number=obs.block_number,
            )
            for obs in observations
        ],
    )


@router.get("/portfolio/holdings", response_model=list[HoldingResponse])
async def get_portfolio_holdings(
    _auth: Annotated[ReadPrincipal, Depends(_require_read_auth)],
    db: AsyncSession = Depends(get_db),
) -> list[HoldingResponse]:
    """Return holdings from the most recent published valuation snapshot.

    Each line includes exact decimal-string quantity and USD valuation where
    available.  Assets without prices have null price_usd / value_usd (unknown
    is never coerced to zero).
    """
    lines = await get_latest_snapshot_lines(db)

    redact = isinstance(_auth, TokenContext) and _auth.redact_addresses
    labels_by_address: dict[str, str | None] = {}
    if redact:
        labels_by_address = {w.address: w.label or None for w in await list_wallets(db)}

    response: list[HoldingResponse] = []
    for line in lines:
        raw_str: str | None = line["raw_amount"]  # type: ignore[assignment]
        decimals: int = int(line["effective_decimals"])  # type: ignore[arg-type]

        quantity_str: str | None = None
        if raw_str is not None:
            try:
                quantity = raw_to_quantity(int(raw_str), decimals)
                quantity_str = format_decimal(quantity)
            except Exception:
                quantity_str = None

        wallet_address = str(line["wallet_address"])
        if redact:
            wallet_address = _redact(wallet_address, labels_by_address.get(wallet_address))

        response.append(
            HoldingResponse(
                wallet_address=wallet_address,
                token_address=str(line["token_address"]),
                symbol=str(line["symbol"]) if line["symbol"] else None,
                decimals=decimals,
                raw_amount=raw_str,
                quantity=quantity_str,
                price_usd=str(line["price_usd"]) if line["price_usd"] else None,
                value_usd=str(line["value_usd"]) if line["value_usd"] else None,
                block_number=int(line["block_number"])
                if line["block_number"] is not None
                else None,
            )
        )
    return response
