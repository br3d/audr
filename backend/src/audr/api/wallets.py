"""FastAPI routes for wallet management (T033 / US1)."""

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
from audr.wallets.service import (
    InvalidAddressError,
    WalletAlreadyExistsError,
    WalletNotFoundError,
    add_wallet,
    get_wallet,
    list_wallets,
    reactivate_wallet,
    set_label,
    stop_wallet,
)

router = APIRouter(prefix="/api/v1")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class AddWalletBody(BaseModel):
    address: str
    label: str = ""


class PatchWalletBody(BaseModel):
    label: str


class WalletResponse(BaseModel):
    id: uuid.UUID
    address: str
    label: str
    status: str
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/wallets", response_model=list[WalletResponse])
async def get_wallets(
    _session: Annotated[Session, Depends(_require_csrf)],
    db: AsyncSession = Depends(get_db),
) -> list[WalletResponse]:
    wallets = await list_wallets(db)
    return [
        WalletResponse(
            id=w.id,
            address=w.address,
            label=w.label,
            status=w.status,
            created_at=w.created_at.isoformat(),
            updated_at=w.updated_at.isoformat(),
        )
        for w in wallets
    ]


@router.post(
    "/wallets",
    response_model=WalletResponse,
    status_code=http_status.HTTP_201_CREATED,
)
async def post_wallet(
    body: AddWalletBody,
    _session: Annotated[Session, Depends(_require_csrf)],
    db: AsyncSession = Depends(get_db),
) -> WalletResponse:
    try:
        wallet = await add_wallet(db, address=body.address, label=body.label)
    except InvalidAddressError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except WalletAlreadyExistsError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Wallet address already tracked",
        ) from exc
    return WalletResponse(
        id=wallet.id,
        address=wallet.address,
        label=wallet.label,
        status=wallet.status,
        created_at=wallet.created_at.isoformat(),
        updated_at=wallet.updated_at.isoformat(),
    )


@router.get("/wallets/{wallet_id}", response_model=WalletResponse)
async def get_wallet_by_id(
    wallet_id: uuid.UUID,
    _session: Annotated[Session, Depends(_require_csrf)],
    db: AsyncSession = Depends(get_db),
) -> WalletResponse:
    wallet = await get_wallet(db, wallet_id=wallet_id)
    if wallet is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Wallet not found",
        )
    return WalletResponse(
        id=wallet.id,
        address=wallet.address,
        label=wallet.label,
        status=wallet.status,
        created_at=wallet.created_at.isoformat(),
        updated_at=wallet.updated_at.isoformat(),
    )


@router.patch("/wallets/{wallet_id}", response_model=WalletResponse)
async def patch_wallet(
    wallet_id: uuid.UUID,
    body: PatchWalletBody,
    _session: Annotated[Session, Depends(_require_csrf)],
    db: AsyncSession = Depends(get_db),
) -> WalletResponse:
    try:
        wallet = await set_label(db, wallet_id=wallet_id, label=body.label)
    except WalletNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Wallet not found"
        ) from exc
    return WalletResponse(
        id=wallet.id,
        address=wallet.address,
        label=wallet.label,
        status=wallet.status,
        created_at=wallet.created_at.isoformat(),
        updated_at=wallet.updated_at.isoformat(),
    )


@router.post("/wallets/{wallet_id}/stop", response_model=WalletResponse)
async def post_wallet_stop(
    wallet_id: uuid.UUID,
    _session: Annotated[Session, Depends(_require_csrf)],
    db: AsyncSession = Depends(get_db),
) -> WalletResponse:
    try:
        wallet = await stop_wallet(db, wallet_id=wallet_id)
    except WalletNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Wallet not found"
        ) from exc
    return WalletResponse(
        id=wallet.id,
        address=wallet.address,
        label=wallet.label,
        status=wallet.status,
        created_at=wallet.created_at.isoformat(),
        updated_at=wallet.updated_at.isoformat(),
    )


@router.post("/wallets/{wallet_id}/reactivate", response_model=WalletResponse)
async def post_wallet_reactivate(
    wallet_id: uuid.UUID,
    _session: Annotated[Session, Depends(_require_csrf)],
    db: AsyncSession = Depends(get_db),
) -> WalletResponse:
    try:
        wallet = await reactivate_wallet(db, wallet_id=wallet_id)
    except WalletNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Wallet not found"
        ) from exc
    return WalletResponse(
        id=wallet.id,
        address=wallet.address,
        label=wallet.label,
        status=wallet.status,
        created_at=wallet.created_at.isoformat(),
        updated_at=wallet.updated_at.isoformat(),
    )
