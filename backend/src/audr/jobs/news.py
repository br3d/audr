"""News-refresh job handler — keyless ingestion (AUD-512, AUD-513).

Polls the feed registry (`news_feed`: project blogs, GitHub release feeds,
and portfolio-wide trade press — docs/news-sources.md §2) with conditional
GET, normalises Atom/RSS into the existing `asset_news` shape, falls back to
the keyless GDELT Doc API for held assets with no native feed, and polls
Snapshot Hub governance proposals for assets explicitly mapped to a space in
`snapshot_space` (docs/news-sources.md §3 — matched by mapping, not symbol
keyword). No step here requires an API key — this retires the
CoinGecko-only path that previously made `news_refresh` a no-op on a fresh
install (AUD-503).

A broken feed (404, moved to an HTML error page, malformed XML), a GDELT
outage, or a Snapshot request/GraphQL failure is recorded as a health
counter / logged and skipped — it must never fail the run for every other
source.
"""

from __future__ import annotations

import asyncio
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
from audr.jobs.news_tracked_registry import list_tracked_asset_symbols
from audr.jobs.snapshot_registry import (
    SnapshotSpace,
    list_due_spaces,
    record_space_failure,
    record_space_success,
)
from audr.providers.feed_parser import FeedItem, FeedParseError, parse_feed
from audr.providers.gdelt import GdeltArticle, GdeltProvider, GdeltRateLimited
from audr.providers.snapshot import SnapshotError, SnapshotProposal, SnapshotProvider

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 15.0
_USER_AGENT = "audr-news-bot/1.0 (+https://github.com/br3d/audr)"
_GDELT_SOURCE = "gdelt"
_SNAPSHOT_SOURCE = "snapshot"

# GDELT breadth-tier throttling (AUD-515) — staging ran ~66 sequential
# queries per run against a keyless, unrated API and hit near-100% 429s,
# burning the whole 900s `news_refresh` cadence for zero rows. Bounding the
# fan-out, spacing calls out, and backing off on a 429 streak keeps a tier
# that may return nothing from consuming the run.
_GDELT_MAX_QUERIES_PER_RUN = 10
_GDELT_MAX_CONSECUTIVE_429 = 3
_GDELT_MIN_SYMBOL_LEN = 3
_GDELT_QUERY_DELAY_S = 1.0

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
        await session.commit()
        return

    due_feeds = await list_due_feeds(session)
    held_symbols = {asset.symbol.upper() for asset in assets}
    tracked_symbols = await list_tracked_asset_symbols(session)

    inserted = 0
    polled = 0
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(_HTTP_TIMEOUT),
        follow_redirects=True,
        headers={"User-Agent": _USER_AGENT},
    ) as client:
        # Commit per feed (AUD-515 P2): a later tier's crash (GDELT, Snapshot)
        # must not roll back feed polls and health counters that already
        # succeeded — staging lost 13 minutes of successful feed polling to
        # exactly that when the GDELT tier ground through 429s mid-run.
        for feed in due_feeds:
            if feed.symbols and not (feed.symbols & held_symbols):
                continue
            polled += 1
            inserted += await _poll_feed(
                session, client=client, feed=feed, assets=assets, tracked_symbols=tracked_symbols
            )
            await session.commit()

        covered = await covered_asset_symbols(session)
        gdelt_assets = [
            a
            for a in assets
            if a.symbol.upper() in tracked_symbols
            and a.symbol.upper() not in covered
            and len(a.symbol) >= _GDELT_MIN_SYMBOL_LEN
        ]
        if gdelt_assets:
            due_now, skipped_this_run = (
                gdelt_assets[:_GDELT_MAX_QUERIES_PER_RUN],
                gdelt_assets[_GDELT_MAX_QUERIES_PER_RUN:],
            )
            if skipped_this_run:
                logger.info(
                    "news_refresh run_id=%s gdelt capped — skipped %d assets this run",
                    run_id,
                    len(skipped_this_run),
                )
            async with GdeltProvider(http_client=client) as gdelt:
                consecutive_429 = 0
                for i, asset in enumerate(due_now):
                    if i > 0:
                        await asyncio.sleep(_GDELT_QUERY_DELAY_S)
                    try:
                        articles = await gdelt.search_news(f'"{asset.symbol}"')
                    except GdeltRateLimited:
                        consecutive_429 += 1
                        logger.info(
                            "news_refresh run_id=%s gdelt rate-limited symbol=%s consecutive=%d",
                            run_id,
                            asset.symbol,
                            consecutive_429,
                        )
                        if consecutive_429 >= _GDELT_MAX_CONSECUTIVE_429:
                            logger.info(
                                "news_refresh run_id=%s gdelt aborting tier after %d"
                                " consecutive 429s",
                                run_id,
                                consecutive_429,
                            )
                            break
                        continue
                    consecutive_429 = 0
                    for article in articles:
                        inserted += await _insert_gdelt_article(
                            session, asset_id=asset.id, article=article
                        )
                    await session.commit()

        due_spaces = await list_due_spaces(session)
        async with SnapshotProvider(http_client=client) as snapshot:
            for space in due_spaces:
                asset_ids = [a.id for a in assets if a.symbol.upper() == space.asset_symbol.upper()]
                if not asset_ids:
                    continue
                inserted += await _poll_snapshot_space(
                    session, snapshot=snapshot, space=space, asset_ids=asset_ids
                )
                await session.commit()

    pruned = await _prune_old_news(session)
    await session.commit()
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
    tracked_symbols: set[str],
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

    target_asset_ids = _targets_for_feed(feed, assets, tracked_symbols)
    retention_cutoff = datetime.now(UTC) - NEWS_RETENTION
    inserted = 0
    for item in items:
        if item.published_at < retention_cutoff:
            # A feed body is the full history upstream still serves, not just
            # what changed — one of these fed 1288 rows for a 16-row effective
            # gain (AUD-573 follow-up). _prune_old_news would delete every one
            # of these anyway; skip the round trip.
            continue
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
    feed: NewsFeed, assets: list[HeldAsset], tracked_symbols: set[str]
) -> Callable[[FeedItem], list[uuid.UUID]]:
    if feed.symbols:
        # Dedup by asset id: each symbol in feed.symbols matches at most one
        # held asset, but guard against the item inserting twice on one
        # asset_id regardless (AUD-571).
        asset_ids = list({a.id for a in assets if a.symbol.upper() in feed.symbols})
        return lambda _item: asset_ids
    # Portfolio-wide press feeds (AUD-515 P1) must resolve to the explicit
    # allow-list, not every held asset — held-ness alone let a meme token
    # ticked 'BITCOIN' and one named 'ASSET' absorb most trade-press rows.
    tracked_assets = [a for a in assets if a.symbol.upper() in tracked_symbols]
    return lambda item: match_assets(item.title, tracked_assets)


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
# Snapshot governance tier (AUD-513)
# ---------------------------------------------------------------------------


async def _poll_snapshot_space(
    session: AsyncSession,
    *,
    snapshot: SnapshotProvider,
    space: SnapshotSpace,
    asset_ids: list[uuid.UUID],
) -> int:
    try:
        proposals = await snapshot.list_proposals(space.space_id)
    except SnapshotError as exc:
        logger.info("news_refresh snapshot space=%s request failed: %s", space.space_id, exc)
        await record_space_failure(session, space_id=space.id, error=str(exc))
        return 0

    await record_space_success(session, space_id=space.id)

    # Same retention gate the feed tier applies (see `_poll_feed`): a space's
    # proposal list is its whole history, and Snapshot spaces that have gone
    # quiet (0031 seeded two whose newest proposal is ~96 days old) served
    # nothing but out-of-retention rows. Without this, every run inserted
    # them and the run's own `_prune_old_news` deleted them again — staging
    # logged a steady `inserted=54 pruned=54` churn per run for zero visible
    # news (AUD-623).
    retention_cutoff = datetime.now(UTC) - NEWS_RETENTION
    inserted = 0
    for proposal in proposals:
        if proposal.start < retention_cutoff:
            continue
        for asset_id in asset_ids:
            inserted += await _upsert_snapshot_proposal(
                session, asset_id=asset_id, proposal=proposal
            )
    return inserted


def _snapshot_title(proposal: SnapshotProposal) -> str:
    """Fold proposal state into the title — a proposal going active -> closed
    must update the existing `asset_news` row rather than hide the change.
    """
    return f"[{proposal.state}] {proposal.title}"


async def _upsert_snapshot_proposal(
    session: AsyncSession, *, asset_id: uuid.UUID, proposal: SnapshotProposal
) -> int:
    return await _upsert_news(
        session,
        asset_id=asset_id,
        source=_SNAPSHOT_SOURCE,
        external_id=proposal.id,
        title=_snapshot_title(proposal),
        url=proposal.link,
        news_site="snapshot",
        published_at=proposal.start,
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


async def _upsert_news(
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
    """Insert, or update in place on a dedup-key conflict.

    Unlike `_insert_news`'s `DO NOTHING`, a source whose items change after
    first being seen (a Snapshot proposal moving active -> closed) must
    update the existing row rather than duplicate it or go stale — the
    `WHERE` clause keeps a no-op re-poll from touching the row at all.
    """
    result = await session.execute(
        sa.text(
            """
            INSERT INTO asset_news
              (asset_id, source, external_id, title, url, news_site,
               thumbnail_url, published_at)
            VALUES
              (:asset_id, :source, :external_id, :title, :url, :news_site,
               :thumbnail_url, :published_at)
            ON CONFLICT (asset_id, source, external_id) DO UPDATE SET
              title = EXCLUDED.title,
              url = EXCLUDED.url,
              news_site = EXCLUDED.news_site
            WHERE asset_news.title IS DISTINCT FROM EXCLUDED.title
               OR asset_news.url IS DISTINCT FROM EXCLUDED.url
               OR asset_news.news_site IS DISTINCT FROM EXCLUDED.news_site
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
