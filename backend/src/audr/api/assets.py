"""FastAPI routes for asset catalog management (AUD-317).

Implements GET /assets, POST /assets/manual, PATCH /assets/{id}
per the http-api.md contract.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi import status as http_status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from audr.api.auth import _require_csrf, _require_session
from audr.assets.catalog import get_latest_catalog_version
from audr.assets.constants import is_native_eth
from audr.auth.models import Session
from audr.db import get_db

router = APIRouter(prefix="/api/v1")

# Matches the vendored source documented in audr.assets.catalog.
_CATALOG_SOURCE = "https://github.com/Uniswap/default-token-list"

# 7 days, matching the asset_icon_refresh job's negative-cache TTL (AUD-385).
_ICON_CACHE_CONTROL = "public, max-age=604800"

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


class CatalogOut(BaseModel):
    source: str
    version: str | None
    count: int
    bundled_at: str | None
    coverage: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_asset_item(row: dict) -> AssetItemOut:
    source = str(row["source"])
    token_address = str(row["token_address"])
    is_native = is_native_eth(token_address)

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


@router.get("/catalog", response_model=CatalogOut)
async def get_catalog(
    _session: Annotated[Session, Depends(_require_session)],
    db: AsyncSession = Depends(get_db),
) -> CatalogOut:
    """Report the pinned catalog snapshot's source, version and coverage.

    No catalog has necessarily been imported yet (the import runs as part of
    discovery) — report zero coverage rather than erroring in that case.
    """
    version = await get_latest_catalog_version(db)
    if version is None:
        return CatalogOut(
            source=_CATALOG_SOURCE,
            version=None,
            count=0,
            bundled_at=None,
            coverage="No catalog snapshot has been imported yet.",
        )
    return CatalogOut(
        source=_CATALOG_SOURCE,
        version=version.commit_hash,
        count=version.entry_count,
        bundled_at=version.imported_at.isoformat(),
        coverage=(
            "Pinned Uniswap default token list snapshot for Ethereum mainnet "
            "(chain_id=1); discovery matches tracked wallets against entries "
            "in this catalog, plus any manually added or on-chain-discovered "
            "assets outside it."
        ),
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


@router.get("/assets/{asset_id}/icon")
async def get_asset_icon(
    asset_id: uuid.UUID,
    _session: Annotated[Session, Depends(_require_session)],
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Serve a cached asset icon (AUD-385), or 404 if none is cached.

    Only ever serves what `asset_icon_refresh` has already cached — this
    route never fetches the upstream inline, so a cold cache 404s (the
    frontend falls back to a generated monogram) instead of slowing down
    the request.
    """
    result = await db.execute(
        sa.text("SELECT content_type, image, status FROM asset_icon WHERE asset_id = :id"),
        {"id": str(asset_id)},
    )
    row = result.first()
    if row is None or row[2] != "ok" or row[1] is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Icon not found")

    content_type, image, _status = row
    return Response(
        content=bytes(image),
        media_type=content_type,
        headers={"Cache-Control": _ICON_CACHE_CONTROL},
    )
