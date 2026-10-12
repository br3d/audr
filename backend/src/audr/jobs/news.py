"""News-refresh job handler — keyless ingestion (AUD-512).

Polls the feed registry (`news_feed`: project blogs, GitHub release feeds,
and portfolio-wide trade press — docs/news-sources.md §2) with conditional
GET, normalises Atom/RSS into the existing `asset_news` shape, and falls
back to the keyless GDELT Doc API for held assets with no native feed. No
step here requires an API key — this retires the CoinGecko-only path that
previously made `news_refresh` a no-op on a fresh install (AUD-503).

A broken feed (404, moved to an HTML error page, malformed XML) or a GDELT
outage is recorded as a health counter / logged and skipped — it must never
fail the run for every other feed.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from audr.jobs.news_feed_registry import (
    NewsFeed,
    covered_asset_symbols,
    list_due_feeds,
    record_feed_failure,
    record_feed_not_modified,
    record_feed_success,
)
from audr.providers.feed_parser import FeedItem, FeedParseError, parse_feed
from audr.providers.gdelt import GdeltArticle, GdeltProvider

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 15.0
_USER_AGENT = "audr-news-bot/1.0 (+https://github.com/br3d/audr)"
_GDELT_SOURCE = "gdelt"

# Retention (AUD-512 req. 6): one shared policy with ADR-007 alerting
# (AUD-337) — do not invent a second number here.
NEWS_RETENTION = timedelta(days=90)


@dataclass(frozen=True)
class HeldAsset:
    id: uuid.UUID
    symbol: str
    name: str


async def handle_news_refresh(session: AsyncSession, run_id: uuid.UUID) -> None:
    """Poll the feed registry and GDELT, caching articles for held assets.

    Requires no API key anywhere in this path.
    """
    assets = await _get_held_assets(session)
    if not assets:
        logger.info("news_refresh skipped — no held assets run_id=%s", run_id)
        await _prune_old_news(session)
        return

    due_feeds = await list_due_feeds(session)
    held_symbols = {asset.symbol.upper() for asset in assets}

    inserted = 0
    polled = 0
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(_HTTP_TIMEOUT),
        follow_redirects=True,
        headers={"User-Agent": _USER_AGENT},
    ) as client:
        for feed in due_feeds:
            if feed.asset_symbol is not None and feed.asset_symbol.upper() not in held_symbols:
                continue
            polled += 1
            inserted += await _poll_feed(session, client=client, feed=feed, assets=assets)

        covered = await covered_asset_symbols(session)
        gdelt_assets = [a for a in assets if a.symbol.upper() not in covered]
        if gdelt_assets:
            async with GdeltProvider(http_client=client) as gdelt:
                for asset in gdelt_assets:
                    articles = await gdelt.search_news(asset.symbol)
                    for article in articles:
                        inserted += await _insert_gdelt_article(
                            session, asset_id=asset.id, article=article
                        )

    pruned = await _prune_old_news(session)
    await session.flush()
    logger.info(
        "news_refresh run_id=%s feeds_polled=%d inserted=%d pruned=%d",
        run_id,
        polled,
        inserted,
        pruned,
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
# Feed polling
# ---------------------------------------------------------------------------


async def _poll_feed(
    session: AsyncSession,
    *,
    client: httpx.AsyncClient,
    feed: NewsFeed,
    assets: list[HeldAsset],
) -> int:
    headers: dict[str, str] = {}
    if feed.etag:
        headers["If-None-Match"] = feed.etag
    if feed.last_modified:
        headers["If-Modified-Since"] = feed.last_modified

    try:
        response = await client.get(feed.url, headers=headers)
    except httpx.HTTPError as exc:
        logger.info("news_refresh feed=%s request failed: %s", feed.key, exc)
        await record_feed_failure(session, feed_id=feed.id, error=str(exc))
        return 0

    if response.status_code == 304:
        await record_feed_not_modified(session, feed_id=feed.id)
        return 0

    if not response.is_success:
        logger.info("news_refresh feed=%s HTTP %s", feed.key, response.status_code)
        await record_feed_failure(session, feed_id=feed.id, error=f"HTTP {response.status_code}")
        return 0

    try:
        items = parse_feed(response.content)
    except FeedParseError as exc:
        logger.info("news_refresh feed=%s parse error: %s", feed.key, exc)
        await record_feed_failure(session, feed_id=feed.id, error=str(exc))
        return 0

    await record_feed_success(
        session,
        feed_id=feed.id,
        etag=response.headers.get("etag"),
        last_modified=response.headers.get("last-modified"),
    )

    target_asset_ids = _targets_for_feed(feed, assets)
    inserted = 0
    for item in items:
        for asset_id in target_asset_ids(item):
            inserted += await _insert_news(
                session,
                asset_id=asset_id,
                source=feed.key,
                external_id=item.external_id,
                title=item.title,
                url=item.url,
                news_site=feed.key,
                published_at=item.published_at,
            )
    return inserted


def _targets_for_feed(
    feed: NewsFeed, assets: list[HeldAsset]
) -> Callable[[FeedItem], list[uuid.UUID]]:
    if feed.asset_symbol is not None:
        asset_ids = [a.id for a in assets if a.symbol.upper() == feed.asset_symbol.upper()]
        return lambda _item: asset_ids
    return lambda item: match_assets(item.title, assets)


# ---------------------------------------------------------------------------
# GDELT breadth tier
# ---------------------------------------------------------------------------


async def _insert_gdelt_article(
    session: AsyncSession, *, asset_id: uuid.UUID, article: GdeltArticle
) -> int:
    return await _insert_news(
        session,
        asset_id=asset_id,
        source=_GDELT_SOURCE,
        external_id=article.external_id,
        title=article.title,
        url=article.url,
        news_site=article.news_site,
        published_at=article.published_at,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _get_held_assets(session: AsyncSession) -> list[HeldAsset]:
    """Return held assets (id, symbol, name) with at least one non-zero balance.

    Excluded assets are included (AUD-447): exclusion scopes what counts
    towards the portfolio's value, and the owner asked for it to change
    nothing outside the dashboard and the Assets list. News about an asset
    you still hold is worth reading whether or not you count it.
    """
    result = await session.execute(
        sa.text(
            """
            SELECT DISTINCT a.id, a.symbol, a.name
            FROM balance_observation bo
            JOIN asset a ON a.id = bo.asset_id
            WHERE bo.raw_amount > 0
            """
        )
    )
    return [HeldAsset(id=row[0], symbol=row[1], name=row[2]) for row in result]


async def _insert_news(
    session: AsyncSession,
    *,
    asset_id: uuid.UUID,
    source: str,
    external_id: str,
    title: str,
    url: str,
    news_site: str,
    published_at: datetime,
    thumbnail_url: str | None = None,
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
            "source": source,
            "external_id": external_id,
            "title": title,
            "url": url,
            "news_site": news_site,
            "thumbnail_url": thumbnail_url,
            "published_at": published_at,
        },
    )
    return result.rowcount or 0


async def _prune_old_news(session: AsyncSession) -> int:
    """Delete asset_news rows older than NEWS_RETENTION. Returns rows deleted."""
    result = await session.execute(
        sa.text("DELETE FROM asset_news WHERE published_at < :cutoff"),
        {"cutoff": datetime.now(tz=UTC) - NEWS_RETENTION},
    )
    return result.rowcount or 0
