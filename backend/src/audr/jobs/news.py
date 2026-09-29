"""News-refresh job handler — fetches and caches news for held assets (AUD-308).

Matches CoinGecko's general /news feed against held assets by symbol/name
keyword, since the Demo tier does not support filtering by coin. Self-throttles
against the most recent fetch to avoid hammering the provider on every poll.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from audr.providers.coingecko_demo import CoinGeckoError, CoinGeckoProvider, NewsItem
from audr.settings.quotes import get_coingecko_api_key

logger = logging.getLogger(__name__)

_SOURCE = "coingecko"
_MIN_REFRESH_INTERVAL = timedelta(minutes=15)


@dataclass(frozen=True)
class HeldAsset:
    id: uuid.UUID
    symbol: str
    name: str


async def handle_news_refresh(session: AsyncSession, run_id: uuid.UUID) -> None:
    """Fetch recent news and cache articles that mention a held asset."""
    api_key = await get_coingecko_api_key(session)
    if not api_key:
        logger.warning(
            "news_refresh skipped — no CoinGecko API key configured run_id=%s", run_id
        )
        return

    assets = await _get_held_assets(session)
    if not assets:
        logger.info("news_refresh skipped — no held assets run_id=%s", run_id)
        return

    if await _fetched_recently(session):
        logger.info("news_refresh skipped — fetched recently run_id=%s", run_id)
        return

    try:
        async with CoinGeckoProvider(api_key=api_key) as provider:
            articles = await provider.get_news()
    except CoinGeckoError as exc:
        logger.warning(
            "news_refresh provider error run_id=%s: %s", run_id, exc
        )
        return

    inserted = 0
    for article in articles:
        for asset_id in match_assets(article.title, assets):
            inserted += await _insert_news(session, asset_id=asset_id, article=article)

    await session.flush()
    logger.info(
        "news_refresh run_id=%s articles=%d inserted=%d",
        run_id,
        len(articles),
        inserted,
    )


def match_assets(title: str, assets: list[HeldAsset]) -> list[uuid.UUID]:
    """Return the ids of held assets whose symbol or name appears in *title*.

    Word-boundary, case-insensitive matching — a symbol like 'UNI' must match
    as a whole word, not as a substring of an unrelated word.
    """
    matched: list[uuid.UUID] = []
    for asset in assets:
        for keyword in (asset.symbol, asset.name):
            if re.search(rf"\b{re.escape(keyword)}\b", title, re.IGNORECASE):
                matched.append(asset.id)
                break
    return matched


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _get_held_assets(session: AsyncSession) -> list[HeldAsset]:
    """Return held assets (id, symbol, name) with at least one non-zero balance."""
    result = await session.execute(
        sa.text(
            """
            SELECT DISTINCT a.id, a.symbol, a.name
            FROM balance_observation bo
            JOIN asset a ON a.id = bo.asset_id
            WHERE bo.raw_amount > 0
              AND NOT COALESCE(a.excluded, false)
            """
        )
    )
    return [HeldAsset(id=row[0], symbol=row[1], name=row[2]) for row in result]


async def _fetched_recently(session: AsyncSession) -> bool:
    result = await session.execute(
        sa.text("SELECT MAX(fetched_at) FROM asset_news WHERE source = :source"),
        {"source": _SOURCE},
    )
    last_fetch = result.scalar_one_or_none()
    if last_fetch is None:
        return False
    return datetime.now(tz=UTC) - last_fetch < _MIN_REFRESH_INTERVAL


async def _insert_news(
    session: AsyncSession, *, asset_id: uuid.UUID, article: NewsItem
) -> int:
    result = await session.execute(
        sa.text(
            """
            INSERT INTO asset_news
              (asset_id, source, external_id, title, url, news_site,
               thumbnail_url, published_at)
            VALUES
              (:asset_id, :source, :external_id, :title, :url, :news_site,
               :thumbnail_url, :published_at)
            ON CONFLICT (asset_id, source, external_id) DO NOTHING
            """
        ),
        {
            "asset_id": str(asset_id),
            "source": _SOURCE,
            "external_id": article.external_id,
            "title": article.title,
            "url": article.url,
            "news_site": article.news_site,
            "thumbnail_url": article.thumbnail_url,
            "published_at": article.published_at,
        },
    )
    return result.rowcount or 0
