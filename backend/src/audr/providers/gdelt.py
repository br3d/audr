"""GDELT 2.0 Doc API adapter — keyless breadth tier for news (AUD-512).

No key, no account, no published rate limit or SLA (docs/news-sources.md
§6): outages and empty results are normal operation here, not errors, so
this adapter never raises — any failure degrades to an empty result and the
caller moves on to the next asset instead of failing the whole job.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
_TIMEOUT = 15.0
_DEFAULT_MAXRECORDS = 10
_DEFAULT_TIMESPAN = "1d"
_USER_AGENT = "audr-news-bot/1.0 (+https://github.com/br3d/audr)"


@dataclass(frozen=True)
class GdeltArticle:
    external_id: str
    title: str
    url: str
    news_site: str
    published_at: datetime


class GdeltProvider:
    """Read-only, keyless GDELT 2.0 Doc API client."""

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

    async def __aenter__(self) -> GdeltProvider:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def search_news(
        self,
        query: str,
        *,
        maxrecords: int = _DEFAULT_MAXRECORDS,
        timespan: str = _DEFAULT_TIMESPAN,
    ) -> list[GdeltArticle]:
        """Return recent articles matching *query*, newest first.

        Never raises: GDELT publishes no rate limit or SLA, so a timeout,
        non-2xx response, or unparseable body is logged and treated as "no
        results this run" rather than failing the whole job.
        """
        try:
            response = await self._client.get(
                self._base,
                params={
                    "query": query,
                    "mode": "artlist",
                    "format": "json",
                    "maxrecords": str(maxrecords),
                    "timespan": timespan,
                    "sort": "datedesc",
                },
            )
        except httpx.HTTPError as exc:
            logger.info("gdelt: request failed for query=%r: %s", query, exc)
            return []

        if not response.is_success:
            logger.info("gdelt: HTTP %s for query=%r", response.status_code, query)
            return []

        try:
            data = response.json()
        except ValueError:
            logger.info("gdelt: unparseable response for query=%r", query)
            return []

        articles = data.get("articles") if isinstance(data, dict) else None
        if not isinstance(articles, list):
            return []

        items: list[GdeltArticle] = []
        for entry in articles:
            if not isinstance(entry, dict):
                continue
            item = _parse_article(entry)
            if item is not None:
                items.append(item)
        return items


def _parse_article(entry: dict[str, Any]) -> GdeltArticle | None:
    url = entry.get("url")
    title = entry.get("title")
    seendate = entry.get("seendate")
    if not url or not title or not seendate:
        return None
    try:
        published_at = datetime.strptime(str(seendate), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        logger.info("gdelt: unparseable seendate: %r", seendate)
        return None
    return GdeltArticle(
        external_id=str(url),
        title=str(title),
        url=str(url),
        news_site=str(entry.get("domain") or "gdelt"),
        published_at=published_at,
    )
