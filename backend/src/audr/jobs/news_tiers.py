"""Shared capped/rate-limited polling loop for news tiers (AUD-691).

The Hacker News (AUD-656) and GDELT (AUD-515) breadth tiers both poll a
keyless, unrated API that fans out one query per held symbol/asset, so both
cap how many queries one `news_refresh` run spends, space queries out, and
back off after a run of consecutive HTTP 429s rather than grinding through
every remaining item one rate limit at a time. `jobs/news.py` used to inline
that loop twice, nearly verbatim; `run_capped_tier` is the one copy.

The two tiers still differ in one way this module preserves rather than
papers over: Hacker News commits after every item, including a rate-limited
one, right up to the abort; GDELT only commits after a successful insert, so
a rate-limited query costs no round trip. `commit_on_rate_limit` is how a
caller picks which of those two shapes it wants.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NewsTier:
    """Per-run throttling for one keyless, query-per-item news tier."""

    name: str
    max_queries_per_run: int
    max_consecutive_429: int
    query_delay_s: float


async def run_capped_tier[T](
    *,
    run_id: uuid.UUID,
    tier: NewsTier,
    items: Sequence[T],
    poll_one: Callable[[T], Awaitable[tuple[int, bool]]],
    commit: Callable[[], Awaitable[None]],
    commit_on_rate_limit: bool = True,
) -> tuple[int, int]:
    """Poll *items* through *poll_one*, capped and rate-limit-aware.

    `poll_one` returns `(rows inserted, was rate limited)` for one item — the
    caller owns how an item is fetched, inserted and what "rate limited"
    means for its own provider. Items beyond `tier.max_queries_per_run` are
    skipped for this run (and remain due for the next one); a run of
    `tier.max_consecutive_429` consecutive rate-limited items aborts the
    rest of the tier. Returns `(rows inserted, items successfully polled)`.
    """
    if len(items) > tier.max_queries_per_run:
        logger.info(
            "news_refresh run_id=%s %s capped — skipped %d items this run",
            run_id,
            tier.name,
            len(items) - tier.max_queries_per_run,
        )
    capped_items = items[: tier.max_queries_per_run]

    inserted = 0
    polled = 0
    consecutive_429 = 0
    for i, item in enumerate(capped_items):
        if i > 0:
            await asyncio.sleep(tier.query_delay_s)

        item_inserted, rate_limited = await poll_one(item)
        inserted += item_inserted

        if rate_limited:
            consecutive_429 += 1
            logger.info(
                "news_refresh run_id=%s %s rate-limited consecutive=%d",
                run_id,
                tier.name,
                consecutive_429,
            )
            if consecutive_429 >= tier.max_consecutive_429:
                logger.info(
                    "news_refresh run_id=%s %s aborting tier after %d consecutive 429s",
                    run_id,
                    tier.name,
                    consecutive_429,
                )
                if commit_on_rate_limit:
                    await commit()
                break
            if commit_on_rate_limit:
                await commit()
            continue

        consecutive_429 = 0
        polled += 1
        await commit()

    return inserted, polled
