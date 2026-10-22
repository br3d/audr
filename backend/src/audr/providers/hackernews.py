"""Hacker News (Algolia Search API) adapter — keyless HN tier (AUD-656).

No key, no account. `hn.algolia.com/api/v1/search_by_date` is a thin wrapper
over an Algolia index: a bare query is typo-tolerant and matches against
every indexed attribute, which for a short, common project name ("Aave",
"Lido") returns mostly unrelated stories — measured live against this API
(AUD-656 wake comment) before writing this adapter. Quoting the query text
*and* restricting the search to the `title` attribute both cut that noise
down sharply (`"Aave"` + `restrictSearchableAttributes=title` returned real
Aave coverage where the unquoted, unrestricted query did not) — the same
quoted-phrase trick the GDELT (`search_news`) and Google News aggregator
(AUD-654) tiers already use for the same reason. It does not fully solve a
project whose name collides with an unrelated noun or brand in common usage
(Polygon the gaming site, "polygon" the shape) — that residual is a
per-feed `block_pattern` problem (AUD-655), not something this adapter can
fix with a better query.

This adapter never raises on an unknown failure, same contract as
`GdeltProvider` — a 429 is the one status surfaced distinctly, via
`HackerNewsRateLimited`, so the caller can back off instead of treating a
rate limit as "no results this run".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_BASE_URL = "https://hn.algolia.com/api/v1/search_by_date"
_TIMEOUT = 15.0
_DEFAULT_HITS_PER_PAGE = 20
_USER_AGENT = "audr-news-bot/1.0 (+https://github.com/br3d/audr)"


class HackerNewsRateLimited(Exception):
    """Raised on HTTP 429 — distinct from the "no results" cases this
    adapter otherwise swallows, so the caller can abort the tier instead of
    treating a rate limit as "nothing happened this run".
    """


@dataclass(frozen=True)
class HackerNewsStory:
    external_id: str
    title: str
    url: str
    points: int
    num_comments: int
    published_at: datetime


class HackerNewsProvider:
    """Read-only, keyless Hacker News Algolia Search API client."""

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        base_url: str = _BASE_URL,
        user_agent: str = _USER_AGENT,
    ) -> None:
        self._base = base_url
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(_TIMEOUT),
            follow_redirects=False,
            headers={"User-Agent": user_agent},
        )

    async def __aenter__(self) -> HackerNewsProvider:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def search_stories(
        self,
        query: str,
        *,
        hits_per_page: int = _DEFAULT_HITS_PER_PAGE,
    ) -> list[HackerNewsStory]:
        """Return stories matching *query*, newest first.

        Raises `HackerNewsRateLimited` on HTTP 429 only. Every other failure
        (timeout, other non-2xx, unparseable body) is logged and treated as
        "no results this run" rather than failing the whole job.
        """
        try:
            response = await self._client.get(
                self._base,
                params={
                    "query": query,
                    "tags": "story",
                    "hitsPerPage": str(hits_per_page),
                    "restrictSearchableAttributes": "title",
                },
            )
        except httpx.HTTPError as exc:
            logger.info("hackernews: request failed for query=%r: %s", query, exc)
            return []

        if response.status_code == 429:
            raise HackerNewsRateLimited(f"429 for query={query!r}")

        if not response.is_success:
            logger.info("hackernews: HTTP %s for query=%r", response.status_code, query)
            return []

        try:
            data = response.json()
        except ValueError:
            logger.info("hackernews: unparseable response for query=%r", query)
            return []

        return parse_search_response(data)


def parse_search_response(data: object) -> list[HackerNewsStory]:
    """Parse an Algolia `search_by_date` JSON body into stories.

    Exposed separately from `search_stories` so a caller holding its own
    `httpx.Response` (the news_refresh job polls `news_feed.url` directly,
    to keep this tier's per-feed health counters driven by the real HTTP
    outcome rather than this adapter's "failure looks like empty" contract)
    does not have to duplicate hit-parsing.
    """
    hits = data.get("hits") if isinstance(data, dict) else None
    if not isinstance(hits, list):
        return []

    items: list[HackerNewsStory] = []
    for entry in hits:
        if not isinstance(entry, dict):
            continue
        item = _parse_hit(entry)
        if item is not None:
            items.append(item)
    return items


def _parse_hit(entry: dict[str, Any]) -> HackerNewsStory | None:
    object_id = entry.get("objectID")
    title = entry.get("title")
    created_at_i = entry.get("created_at_i")
    if not object_id or not title or created_at_i is None:
        return None
    try:
        published_at = datetime.fromtimestamp(int(created_at_i), tz=UTC)
    except (TypeError, ValueError, OSError):
        logger.info("hackernews: unparseable created_at_i: %r", created_at_i)
        return None
    url = entry.get("url") or f"https://news.ycombinator.com/item?id={object_id}"
    return HackerNewsStory(
        external_id=str(object_id),
        title=str(title),
        url=str(url),
        points=int(entry.get("points") or 0),
        num_comments=int(entry.get("num_comments") or 0),
        published_at=published_at,
    )
