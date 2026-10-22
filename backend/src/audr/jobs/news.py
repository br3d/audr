"""News-refresh job handler — keyless ingestion (AUD-512, AUD-513, AUD-654).

Polls the feed registry (`news_feed`: project blogs, GitHub release feeds,
portfolio-wide trade press, and a per-symbol Google News breadth tier —
docs/news-sources.md §2/§9) with conditional GET, normalises Atom/RSS into
the existing `asset_news` shape, falls back to the keyless GDELT Doc API for
held assets with no native feed, and polls Snapshot Hub governance proposals
for assets explicitly mapped to a space in `snapshot_space`
(docs/news-sources.md §3 — matched by mapping, not symbol keyword). No step
here requires an API key — this retires the CoinGecko-only path that
previously made `news_refresh` a no-op on a fresh install (AUD-503).

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

# Release-tier noise (AUD-640). Backstop only: 0038 disabled every
# release-tier feed in the registry, because a `releases.atom` entry's title
# *is* the version tag — `v2.10.3`, `@ensdomains/ensjs@5.2.4` — and a bare
# tag is unreadable on a portfolio card whether or not the release itself
# matters. Coverage moved to project feeds (docs/news-sources.md §9).
#
# This is the one noise rule AUD-655 left in code rather than moving to
# `news_feed.block_pattern`: every other rule (the project-feed-scoped half
# of this same filter, the aggregator tier's evergreen-explainer filter) was
# already opt-in by key or by tier membership, so turning it into a column
# changes nothing about who has to remember to configure it. A release feed
# is different — it needs no opt-in, so re-enabling one or seeding a new one
# must not quietly reintroduce the two worst shapes (monorepo package tags,
# pre-release/test builds) just because nobody remembered to set that feed's
# `block_pattern`. Filtered here rather than in the API or the UI so a
# dropped row costs no retention, no alert pass and no card slot.
_PACKAGE_TAG_RE = re.compile(r"@v?\d+\.\d+")
_PRERELEASE_RE = re.compile(
    r"(?<![a-z])(?:alpha|beta|rc|nightly|canary|preconf|pre-?release|unstable)(?![a-z])"
    r"|testing only",
    re.IGNORECASE,
)


def is_noisy_release(title: str) -> bool:
    """True when a release-feed entry is a package tag or a pre-release build."""
    return bool(_PACKAGE_TAG_RE.search(title) or _PRERELEASE_RE.search(title))


def _compile_feed_pattern(
    feed_key: str, kind: str, pattern: str | None
) -> tuple[re.Pattern[str] | None, bool]:
    """Compile a feed's `keep_pattern`/`block_pattern` (AUD-655).

    A pattern is operator-entered data, not application code: a broken one
    must not fail `news_refresh` for every feed. Returns `(None, True)` and
    logs a warning on `re.error` — the caller treats the feed as if the
    pattern were unset for this poll.
    """
    if pattern is None:
        return None, False
    try:
        return re.compile(pattern, re.IGNORECASE), False
    except re.error as exc:
        logger.warning(
            "news_refresh feed=%s invalid %s_pattern %r: %s", feed_key, kind, pattern, exc
        )
        return None, True


def should_drop_feed_item(
    *,
    tier: str,
    key: str,
    title: str,
    keep_re: re.Pattern[str] | None = None,
    block_re: re.Pattern[str] | None = None,
) -> bool:
    """True when a feed item is noise rather than news.

    Order is keep, then block (AUD-655): a configured `keep_re` is an
    allow-list, so a title that fails it is dropped before `block_re` is even
    consulted. `is_noisy_release()` — see its docstring — is the one filter
    that stays in code rather than in a feed's `block_pattern`, and it
    applies to the `release` tier unconditionally, after the DB-sourced
    patterns.
    """
    if keep_re is not None and not keep_re.search(title):
        return True
    if block_re is not None and block_re.search(title):
        return True
    return tier == "release" and is_noisy_release(title)


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
    pattern_errors = 0
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
            feed_inserted, feed_pattern_errors = await _poll_feed(
                session, client=client, feed=feed, assets=assets, tracked_symbols=tracked_symbols
            )
            inserted += feed_inserted
            pattern_errors += feed_pattern_errors
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
        "news_refresh run_id=%s feeds_polled=%d inserted=%d pruned=%d pattern_errors=%d",
        run_id,
        polled,
        inserted,
        pruned,
        pattern_errors,
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
) -> tuple[int, int]:
    """Poll one feed; returns (rows inserted, keep/block patterns that failed to compile)."""
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
        return 0, 0

    if response.status_code == 304:
        await record_feed_not_modified(session, feed_id=feed.id)
        return 0, 0

    if not response.is_success:
        logger.info("news_refresh feed=%s HTTP %s", feed.key, response.status_code)
        await record_feed_failure(session, feed_id=feed.id, error=f"HTTP {response.status_code}")
        return 0, 0

    try:
        items = parse_feed(response.content)
    except FeedParseError as exc:
        logger.info("news_refresh feed=%s parse error: %s", feed.key, exc)
        await record_feed_failure(session, feed_id=feed.id, error=str(exc))
        return 0, 0

    await record_feed_success(
        session,
        feed_id=feed.id,
        etag=response.headers.get("etag"),
        last_modified=response.headers.get("last-modified"),
    )

    keep_re, keep_error = _compile_feed_pattern(feed.key, "keep", feed.keep_pattern)
    block_re, block_error = _compile_feed_pattern(feed.key, "block", feed.block_pattern)
    pattern_errors = int(keep_error) + int(block_error)

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
        if should_drop_feed_item(
            tier=feed.tier, key=feed.key, title=item.title, keep_re=keep_re, block_re=block_re
        ):
            # DB-sourced keep/block patterns plus the release-tier backstop
            # (AUD-655) — see should_drop_feed_item.
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
    return inserted, pattern_errors


def _targets_for_feed(
    feed: NewsFeed, assets: list[HeldAsset], tracked_symbols: set[str]
) -> Callable[[FeedItem], list[uuid.UUID]]:
    if feed.symbols:
        scoped_assets = [a for a in assets if a.symbol.upper() in feed.symbols]
        if feed.tier == "aggregator":
            # A quoted Google News search is not attribution-exact the way a
            # project blog is (AUD-654) — gate on the same symbol-or-name
            # match the press tier uses, reusing match_assets rather than a
            # second mechanism, scoped to this feed's own asset(s) only.
            return lambda item: match_assets(item.title, scoped_assets)
        # Dedup by asset id: each symbol in feed.symbols matches at most one
        # held asset, but guard against the item inserting twice on one
        # asset_id regardless (AUD-571).
        asset_ids = list({a.id for a in scoped_assets})
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
