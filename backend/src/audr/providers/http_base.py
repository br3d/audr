"""Shared keyless-provider HTTP plumbing (AUD-691).

Every adapter under `audr.providers` that talks to a keyless third-party API
hand-rolled its own httpx.AsyncClient construction/timeout and
__aenter__/__aexit__/close bookkeeping around the same "own the client
unless one is injected" rule, and the news tiers (GDELT, Hacker News,
Snapshot — plus `jobs/news.py`'s own shared client) each repeated the
identical User-Agent string and a near-identical "raise on 429, swallow
everything else" exception. Centralising that plumbing here leaves each
provider responsible only for its base URL, its parsing, and (where it
genuinely differs, as CoinGecko/CoinMarketCap's quote errors do) its own
exception hierarchy.
"""

from __future__ import annotations

import httpx

DEFAULT_TIMEOUT = 15.0

# Identifies audr's background jobs to upstream keyless APIs (GDELT, Hacker
# News/Algolia, Snapshot Hub) — previously copy-pasted across their adapters
# plus jobs/news.py's own shared client.
NEWS_USER_AGENT = "audr-news-bot/1.0 (+https://github.com/br3d/audr)"


class RateLimitError(Exception):
    """Raised on HTTP 429 by a provider that distinguishes it from every
    other failure, which it otherwise swallows as "no results this run"
    rather than failing the caller's whole run.

    Shared by the GDELT and Hacker News adapters, which previously each
    defined their own identically-shaped exception for exactly this.
    """


class ProviderHttpBase:
    """Owns (or borrows) an httpx.AsyncClient for one provider adapter.

    An externally supplied *http_client* (tests, or a client shared across
    several providers within one job run) is always the caller's to close —
    `close()` only tears down a client this instance created itself.
    """

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        user_agent: str | None = None,
        follow_redirects: bool = False,
    ) -> None:
        self._owns_client = http_client is None
        headers = {"User-Agent": user_agent} if user_agent else {}
        self._client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            follow_redirects=follow_redirects,
            headers=headers,
        )

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client

    async def __aenter__(self) -> ProviderHttpBase:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
