"""FastAPI routes for wallet management (T033 / US1)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from audr.api.auth import _require_csrf, _require_session
from audr.auth.models import Session
from audr.db import get_db
from audr.wallets.models import Wallet
from audr.wallets.service import (
    InvalidAddressError,
    WalletAlreadyExistsError,
    WalletNotFoundError,
    add_wallet,
    delete_wallet,
    get_wallet,
    list_wallets_page,
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
    chain_id: int = 1  # accepted but ignored — only mainnet is supported


class PatchWalletBody(BaseModel):
    label: str | None = None
    tracking_active: bool | None = None


class WalletCoverageOut(BaseModel):
    status: str
    catalog_attempted: int | None = None
    catalog_total: int | None = None
    completed_at: str | None = None


class WalletOut(BaseModel):
    id: str
    address: str
    label: str | None
    chain_id: int
    tracking_active: bool
    coverage: WalletCoverageOut | None
    created_at: str


class DeleteWalletOut(BaseModel):
    wallet_id: str
    # table name -> rows removed, so the UI can say what was actually purged
    deleted: dict[str, int]


class WalletsListOut(BaseModel):
    items: list[WalletOut]
    next_cursor: str | None
    request_id: str
    generated_at: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wallet_to_out(wallet: Wallet) -> WalletOut:
    return WalletOut(
        id=str(wallet.id),
        address=wallet.address,
        label=wallet.label or None,
        chain_id=1,
        tracking_active=(wallet.status == "active"),
        coverage=None,
        created_at=wallet.created_at.isoformat(),
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/wallets", response_model=WalletsListOut)
async def get_wallets(
    _session: Annotated[Session, Depends(_require_session)],
    db: AsyncSession = Depends(get_db),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
) -> WalletsListOut:
    cursor_uuid: uuid.UUID | None = None
    if cursor is not None:
        try:
            cursor_uuid = uuid.UUID(cursor)
        except ValueError as exc:
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail="cursor is not a valid UUID",
            ) from exc

    wallets, next_cursor = await list_wallets_page(db, cursor=cursor_uuid, limit=limit)
    now = datetime.now(tz=UTC)
    return WalletsListOut(
        items=[_wallet_to_out(w) for w in wallets],
        next_cursor=str(next_cursor) if next_cursor is not None else None,
        request_id=str(uuid.uuid4()),
        generated_at=now.isoformat(),
    )


@router.post(
    "/wallets",
    response_model=WalletOut,
    status_code=http_status.HTTP_201_CREATED,
)
async def post_wallet(
    body: AddWalletBody,
    _session: Annotated[Session, Depends(_require_csrf)],
    db: AsyncSession = Depends(get_db),
) -> WalletOut:
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
    return _wallet_to_out(wallet)


@router.get("/wallets/{wallet_id}", response_model=WalletOut)
async def get_wallet_by_id(
    wallet_id: uuid.UUID,
    _session: Annotated[Session, Depends(_require_session)],
    db: AsyncSession = Depends(get_db),
) -> WalletOut:
    wallet = await get_wallet(db, wallet_id=wallet_id)
    if wallet is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Wallet not found",
        )
    return _wallet_to_out(wallet)


@router.patch("/wallets/{wallet_id}", response_model=WalletOut)
async def patch_wallet(
    wallet_id: uuid.UUID,
    body: PatchWalletBody,
    _session: Annotated[Session, Depends(_require_csrf)],
    db: AsyncSession = Depends(get_db),
) -> WalletOut:
    try:
        wallet = await get_wallet(db, wallet_id=wallet_id)
        if wallet is None:
            raise WalletNotFoundError(str(wallet_id))

        if body.tracking_active is not None:
            if body.tracking_active:
                wallet = await reactivate_wallet(db, wallet_id=wallet_id)
            else:
                wallet = await stop_wallet(db, wallet_id=wallet_id)

        if body.label is not None:
            wallet = await set_label(db, wallet_id=wallet_id, label=body.label)

    except WalletNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Wallet not found"
        ) from exc
    return _wallet_to_out(wallet)


@router.delete("/wallets/{wallet_id}", response_model=DeleteWalletOut)
async def delete_wallet_by_id(
    wallet_id: uuid.UUID,
    _session: Annotated[Session, Depends(_require_csrf)],
    db: AsyncSession = Depends(get_db),
) -> DeleteWalletOut:
    """Permanently remove a wallet and everything derived from it (AUD-367).

    This is distinct from ``POST /wallets/{id}/stop``, which only pauses
    scanning and keeps the address and its history.
    """
    try:
        deleted = await delete_wallet(db, wallet_id=wallet_id)
    except WalletNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Wallet not found"
        ) from exc
    return DeleteWalletOut(wallet_id=str(wallet_id), deleted=deleted)


@router.post("/wallets/{wallet_id}/stop", response_model=WalletOut)
async def post_wallet_stop(
    wallet_id: uuid.UUID,
    _session: Annotated[Session, Depends(_require_csrf)],
    db: AsyncSession = Depends(get_db),
) -> WalletOut:
    try:
        wallet = await stop_wallet(db, wallet_id=wallet_id)
    except WalletNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Wallet not found"
        ) from exc
    return _wallet_to_out(wallet)


@router.post("/wallets/{wallet_id}/reactivate", response_model=WalletOut)
async def post_wallet_reactivate(
    wallet_id: uuid.UUID,
    _session: Annotated[Session, Depends(_require_csrf)],
    db: AsyncSession = Depends(get_db),
) -> WalletOut:
    try:
        wallet = await reactivate_wallet(db, wallet_id=wallet_id)
    except WalletNotFoundError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Wallet not found"
        ) from exc
    return _wallet_to_out(wallet)
