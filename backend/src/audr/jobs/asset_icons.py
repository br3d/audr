"""Asset icon refresh job — populates the keyless icon cache for held assets (AUD-385).

Mirrors the held-asset-only pattern already used by `quote_refresh` and
`news_refresh`: only assets with a non-zero balance observation are
candidates, so an asset that was merely discovered but never actually held
never costs an outbound request.

Runs out of the request path on purpose — `GET /assets/{id}/icon` only ever
serves what this job has already cached, so a cold cache degrades to the
frontend's generated monogram instead of slowing down a dashboard render.

Negative caching (rotki's `failed_asset_ids`): an asset neither keyless
upstream has an icon for is recorded with `status='missing'` and is not
retried until `_NEGATIVE_CACHE_TTL` has elapsed. A CoinGecko 429 is handled
separately — it leaves no row behind at all, so the asset is retried on the
very next run rather than being negative-cached.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta

import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from audr.assets.constants import is_native_eth
from audr.config import get_settings
from audr.jobs.policy import RateLimiter, get_shared_asset_icon_rate_limiter
from audr.providers.asset_icons import (
    IconFetchError,
    IconImage,
    IconRateLimitedError,
    fetch_coingecko_icon,
    fetch_trust_wallet_icon,
)

logger = logging.getLogger(__name__)

# Caps how many assets a single run resolves, so one run can't balloon into
# an unbounded burst of outbound requests — the rest are picked up by the
# next run, same cadence as event_indexer's chunk cap (AUD-362).
_MAX_ASSETS_PER_RUN = 20

# How long a 'missing' row is trusted before the job tries that asset's icon
# again (AUD-385 acceptance: 7 days is fine).
_NEGATIVE_CACHE_TTL = timedelta(days=7)


async def handle_asset_icon_refresh(session: AsyncSession, run_id: uuid.UUID) -> None:
    """Resolve and cache icons for held assets that have none cached yet."""
    settings = get_settings()
    if not settings.asset_icons_remote_fetch:
        logger.info(
            "asset_icon_refresh skipped — asset_icons_remote_fetch disabled run_id=%s",
            run_id,
        )
        return

    candidates = await _get_assets_needing_icon(session)
    if not candidates:
        logger.info("asset_icon_refresh skipped — no assets need an icon run_id=%s", run_id)
        return

    rate_limiter = get_shared_asset_icon_rate_limiter()
    resolved = 0
    missing = 0
    rate_limited = 0

    async with httpx.AsyncClient(follow_redirects=True) as client:
        for asset_id, token_address in candidates:
            try:
                icon = await _resolve_icon(client, token_address, rate_limiter=rate_limiter)
            except IconRateLimitedError:
                rate_limited += 1
                logger.info("asset_icon_refresh rate limited asset=%s run_id=%s", asset_id, run_id)
                continue

            if icon is not None:
                await _upsert_icon(
                    session,
                    asset_id=asset_id,
                    content_type=icon.content_type,
                    image=icon.data,
                    source=icon.source,
                    status="ok",
                )
                resolved += 1
            else:
                await _upsert_icon(
                    session,
                    asset_id=asset_id,
                    content_type=None,
                    image=None,
                    source=None,
                    status="missing",
                )
                missing += 1

    await session.flush()
    logger.info(
        "asset_icon_refresh run_id=%s candidates=%d resolved=%d missing=%d rate_limited=%d",
        run_id,
        len(candidates),
        resolved,
        missing,
        rate_limited,
    )


async def _resolve_icon(
    client: httpx.AsyncClient, token_address: str, *, rate_limiter: RateLimiter
) -> IconImage | None:
    """Try Trust Wallet first, then (for ERC-20s only) the keyless CoinGecko fallback."""
    icon = await fetch_trust_wallet_icon(client, token_address)
    if icon is not None or is_native_eth(token_address):
        return icon
    try:
        return await fetch_coingecko_icon(client, token_address, rate_limiter=rate_limiter)
    except IconFetchError as exc:
        if isinstance(exc, IconRateLimitedError):
            raise
        logger.info("asset_icon_refresh: coingecko fetch error for %s: %s", token_address, exc)
        return None


async def _get_assets_needing_icon(
    session: AsyncSession,
) -> list[tuple[uuid.UUID, str]]:
    """Return (asset_id, token_address) for held assets with no fresh icon cache row.

    "Held" mirrors quote_refresh/news_refresh: at least one non-zero balance
    observation, excluded or not (AUD-447) — the Assets list still renders
    excluded rows behind "Show excluded", so they still need an icon.
    Excludes assets already cached with status='ok', and assets
    negative-cached ('missing') more recently than `_NEGATIVE_CACHE_TTL`.
    """
    result = await session.execute(
        sa.text(
            """
            SELECT DISTINCT a.id, a.token_address
            FROM balance_observation bo
            JOIN asset a ON a.id = bo.asset_id
            LEFT JOIN asset_icon ai ON ai.asset_id = a.id
            WHERE bo.raw_amount > 0
              AND (
                ai.asset_id IS NULL
                OR (ai.status = 'missing' AND ai.fetched_at < now() - :ttl)
              )
            LIMIT :limit
            """
        ),
        {"ttl": _NEGATIVE_CACHE_TTL, "limit": _MAX_ASSETS_PER_RUN},
    )
    return [(row[0], row[1]) for row in result]


async def _upsert_icon(
    session: AsyncSession,
    *,
    asset_id: uuid.UUID,
    content_type: str | None,
    image: bytes | None,
    source: str | None,
    status: str,
) -> None:
    await session.execute(
        sa.text(
            """
            INSERT INTO asset_icon (asset_id, content_type, image, source, status, fetched_at)
            VALUES (:asset_id, :content_type, :image, :source, :status, now())
            ON CONFLICT (asset_id) DO UPDATE SET
                content_type = EXCLUDED.content_type,
                image = EXCLUDED.image,
                source = EXCLUDED.source,
                status = EXCLUDED.status,
                fetched_at = EXCLUDED.fetched_at
            """
        ),
        {
            "asset_id": str(asset_id),
            "content_type": content_type,
            "image": image,
            "source": source,
            "status": status,
        },
    )
