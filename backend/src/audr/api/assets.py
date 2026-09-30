"""FastAPI routes for asset catalog management (AUD-317).

Implements GET /assets, POST /assets/manual, PATCH /assets/{id}
per the http-api.md contract.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from audr.api.auth import _require_csrf, _require_session
from audr.auth.models import Session
from audr.db import get_db

router = APIRouter(prefix="/api/v1")

# Sentinel address used to represent native ETH in the asset table.
_NATIVE_ETH_ADDRESS = "0x0000000000000000000000000000000000000000"

_SOURCE_TO_METADATA_SOURCE: dict[str, str] = {
    "catalog": "catalog",
    "manual": "owner",
    "chain": "chain",
}

_SOURCE_TO_KIND: dict[str, str] = {
    "catalog": "catalog",
    "manual": "manual",
    "chain": "discovered",
}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class AssetItemOut(BaseModel):
    id: str
    chain_id: int
    kind: str
    contract_address: str | None
    symbol: str
    name: str
    decimals: int | None
    excluded: bool
    metadata_source: str
    has_metadata_conflict: bool
    created_at: str


class AssetsResponseOut(BaseModel):
    items: list[AssetItemOut]
    next_cursor: str | None
    request_id: str
    generated_at: str


class AddManualAssetBody(BaseModel):
    contract_address: str
    decimals_override: int | None = None
    symbol_override: str | None = None


class PatchAssetBody(BaseModel):
    excluded: bool | None = None
    decimals_override: int | None = None
    confirm_metadata_override: bool | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_asset_item(row: dict) -> AssetItemOut:
    source = str(row["source"])
    token_address = str(row["token_address"])
    is_native = token_address == _NATIVE_ETH_ADDRESS

    kind: str
    if is_native:
        kind = "native"
    else:
        kind = _SOURCE_TO_KIND.get(source, "manual")

    metadata_source = _SOURCE_TO_METADATA_SOURCE.get(source, "owner")
    contract_address: str | None = None if is_native else token_address

    decimals_override = row.get("decimals_override")
    effective_decimals = int(decimals_override) if decimals_override is not None else int(row["decimals"])

    created_at = row["created_at"]
    if hasattr(created_at, "isoformat"):
        created_at = created_at.isoformat()

    return AssetItemOut(
        id=str(row["id"]),
        chain_id=1,
        kind=kind,
        contract_address=contract_address,
        symbol=str(row["symbol"]),
        name=str(row["name"]),
        decimals=effective_decimals,
        excluded=bool(row["excluded"]),
        metadata_source=metadata_source,
        has_metadata_conflict=bool(row.get("has_conflict", False)),
        created_at=created_at,
    )


async def _get_asset_by_id(db: AsyncSession, asset_id: uuid.UUID) -> dict | None:
    result = await db.execute(
        sa.text(
            """
            SELECT
                a.id, a.token_address, a.symbol, a.name, a.decimals,
                a.source, a.excluded, a.decimals_override, a.created_at,
                EXISTS(
                    SELECT 1 FROM asset_metadata_revision r
                    WHERE r.asset_id = a.id
                      AND (r.symbol != a.symbol OR r.decimals != a.decimals)
                ) AS has_conflict
            FROM asset a
            WHERE a.id = :id
            """
        ),
        {"id": str(asset_id)},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/assets", response_model=AssetsResponseOut)
async def list_assets(
    _session: Annotated[Session, Depends(_require_session)],
    db: AsyncSession = Depends(get_db),
    excluded: bool | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> AssetsResponseOut:
    """List assets with optional exclusion filter and cursor pagination."""
    if limit > 200:
        limit = 200

    conditions = []
    params: dict = {"limit": limit + 1}

    if excluded is not None:
        conditions.append("a.excluded = :excluded")
        params["excluded"] = excluded

    if cursor:
        conditions.append("a.id > :cursor")
        params["cursor"] = cursor

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    result = await db.execute(
        sa.text(
            f"""
            SELECT
                a.id, a.token_address, a.symbol, a.name, a.decimals,
                a.source, a.excluded, a.decimals_override, a.created_at,
                EXISTS(
                    SELECT 1 FROM asset_metadata_revision r
                    WHERE r.asset_id = a.id
                      AND (r.symbol != a.symbol OR r.decimals != a.decimals)
                ) AS has_conflict
            FROM asset a
            {where_clause}
            ORDER BY a.id
            LIMIT :limit
            """
        ),
        params,
    )
    rows = [dict(r) for r in result.mappings()]

    next_cursor: str | None = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = str(rows[-1]["id"])

    now = datetime.now(tz=UTC)
    return AssetsResponseOut(
        items=[_row_to_asset_item(r) for r in rows],
        next_cursor=next_cursor,
        request_id=str(uuid.uuid4()),
        generated_at=now.isoformat(),
    )


@router.post("/assets/manual", response_model=AssetItemOut, status_code=http_status.HTTP_201_CREATED)
async def add_manual_asset(
    body: AddManualAssetBody,
    _session: Annotated[Session, Depends(_require_csrf)],
    db: AsyncSession = Depends(get_db),
) -> AssetItemOut:
    """Add a manually tracked ERC-20 asset.

    Returns the existing asset (409) if the contract_address is already tracked.
    """
    address = body.contract_address.lower()

    # Check for existing asset with this token_address.
    existing = await db.execute(
        sa.text(
            """
            SELECT
                a.id, a.token_address, a.symbol, a.name, a.decimals,
                a.source, a.excluded, a.decimals_override, a.created_at,
                false AS has_conflict
            FROM asset a
            WHERE a.token_address = :addr
            """
        ),
        {"addr": address},
    )
    existing_row = existing.mappings().first()
    if existing_row is not None:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail={
                "code": "asset_already_exists",
                "message": "Asset with this contract address already exists.",
                "existing_id": str(existing_row["id"]),
            },
        )

    symbol = body.symbol_override or "UNKNOWN"
    asset_id = uuid.uuid4()
    now = datetime.now(tz=UTC)

    await db.execute(
        sa.text(
            """
            INSERT INTO asset
              (id, token_address, symbol, name, decimals, source, excluded,
               decimals_override, created_at, updated_at)
            VALUES
              (:id, :addr, :symbol, :name, :decimals, 'manual', false,
               :decimals_override, :now, :now)
            """
        ),
        {
            "id": str(asset_id),
            "addr": address,
            "symbol": symbol,
            "name": symbol,
            "decimals": body.decimals_override if body.decimals_override is not None else 18,
            "decimals_override": body.decimals_override,
            "now": now,
        },
    )
    await db.commit()

    row = await _get_asset_by_id(db, asset_id)
    if row is None:
        raise HTTPException(status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Asset insert failed")
    return _row_to_asset_item(row)


@router.patch("/assets/{asset_id}", response_model=AssetItemOut)
async def patch_asset(
    asset_id: uuid.UUID,
    body: PatchAssetBody,
    _session: Annotated[Session, Depends(_require_csrf)],
    db: AsyncSession = Depends(get_db),
) -> AssetItemOut:
    """Update an asset's exclusion state or decimals override."""
    row = await _get_asset_by_id(db, asset_id)
    if row is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Asset not found")

    updates: list[str] = []
    params: dict = {"id": str(asset_id), "now": datetime.now(tz=UTC)}

    if body.excluded is not None:
        updates.append("excluded = :excluded")
        params["excluded"] = body.excluded

    if body.decimals_override is not None:
        updates.append("decimals_override = :decimals_override")
        params["decimals_override"] = body.decimals_override

    if updates:
        updates.append("updated_at = :now")
        await db.execute(
            sa.text(f"UPDATE asset SET {', '.join(updates)} WHERE id = :id"),
            params,
        )
        await db.commit()

    updated = await _get_asset_by_id(db, asset_id)
    if updated is None:
        raise HTTPException(status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Asset update failed")
    return _row_to_asset_item(updated)
