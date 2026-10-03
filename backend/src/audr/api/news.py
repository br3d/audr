"""FastAPI routes for per-asset news (AUD-308 / AUD-301).

GET /api/v1/assets/{asset_id}/news
  Returns a paginated list of cached news articles linked to the given
  asset, newest first. Articles are populated by the NEWS_REFRESH background
  job (audr.jobs.news) — this endpoint only ever reads the cache.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from audr.api.auth import _require_session
from audr.auth.models import Session
from audr.db import get_db

router = APIRouter(prefix="/api/v1")

_MAX_LIMIT = 100
_DEFAULT_LIMIT = 20


class AssetNewsItem(BaseModel):
    id: str
    source: str
    title: str
    url: str
    news_site: str
    thumbnail_url: str | None
    published_at: str
    fetched_at: str


class AssetNewsResponse(BaseModel):
    asset_id: str
    total: int
    limit: int
    offset: int
    news: list[AssetNewsItem]


@router.get("/assets/{asset_id}/news", response_model=AssetNewsResponse)
async def get_asset_news(
    asset_id: str,
    _session: Annotated[Session, Depends(_require_session)],
    db: AsyncSession = Depends(get_db),
    limit: int = _DEFAULT_LIMIT,
    offset: int = 0,
) -> AssetNewsResponse:
    """Return cached news articles for *asset_id*, newest first."""
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
    try:
        asset_uuid = uuid.UUID(asset_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="asset_id is not a valid UUID",
        ) from exc

    exists = await db.execute(
        sa.text("SELECT 1 FROM asset WHERE id = :id"), {"id": str(asset_uuid)}
    )
    if exists.first() is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="asset not found")

    count_row = await db.execute(
        sa.text("SELECT COUNT(*) FROM asset_news WHERE asset_id = :id"),
        {"id": str(asset_uuid)},
    )
    total = count_row.scalar_one()

    rows = await db.execute(
        sa.text(
            """
            SELECT id, source, title, url, news_site, thumbnail_url,
                   published_at, fetched_at
            FROM asset_news
            WHERE asset_id = :id
            ORDER BY published_at DESC
            LIMIT :limit OFFSET :offset
            """
        ),
        {"id": str(asset_uuid), "limit": limit, "offset": offset},
    )

    news = [
        AssetNewsItem(
            id=str(row[0]),
            source=str(row[1]),
            title=str(row[2]),
            url=str(row[3]),
            news_site=str(row[4]),
            thumbnail_url=row[5],
            published_at=row[6].isoformat(),
            fetched_at=row[7].isoformat(),
        )
        for row in rows.fetchall()
    ]

    return AssetNewsResponse(
        asset_id=str(asset_uuid),
        total=total,
        limit=limit,
        offset=offset,
        news=news,
    )
