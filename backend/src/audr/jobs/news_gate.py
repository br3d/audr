"""The "smart analysis" ingest gate (ADR-007 phase 3, AUD-658).

Founder's semantics (AUD-658 wake comment, card `bea3dbdc`, 2026-10-21): one
global flag. On, every article is scored for significance before it reaches
`asset_news`; what scores below the threshold lands in `news_suppressed`
instead (so "why did news stop showing up" has an answer) rather than being
discarded outright. Off (the default) is a pure pass-through — see
`NewsAnalysisGate.ingest`'s first branch — so ingestion with the flag off
is byte-for-byte what `audr.jobs.news` always did.

Fail-open is the one acceptance-critical property (AUD-658): a missing key,
a timeout, a 429, a malformed response, or the run's own budget ceiling must
never drop an article or stop the run — they all fall through to the same
plain insert as the flag-off path. Only a *completed* classification that
scores below the threshold is allowed to suppress.

`NewsAnalysisGate.ingest` takes the actual "insert into asset_news" as an
`insert` callback (`audr.jobs.news._insert_news`) rather than importing it,
since `audr.jobs.news` is this module's only caller and would otherwise form
an import cycle.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from audr.config import get_settings
from audr.providers.llm import AnthropicClient, LlmUnavailable
from audr.settings.llm import get_anthropic_api_key
from audr.settings.news_analysis import get_news_analysis_setting

logger = logging.getLogger(__name__)

# Debugging-memory window, not history (AUD-658): short on purpose relative
# to asset_news/news_alert's 90-day retention (audr.jobs.news.NEWS_RETENTION)
# — this table exists to answer "why did an article not show up recently",
# not to be a durable record of every near-miss.
SUPPRESSED_RETENTION_DAYS = 30

InsertFn = Callable[..., Awaitable[int]]


@dataclass
class _RunBudget:
    limit_usd: Decimal
    spent_usd: Decimal = field(default_factory=lambda: Decimal(0))

    def has_room(self) -> bool:
        return self.spent_usd < self.limit_usd

    def charge(self, cost_usd: Decimal) -> None:
        self.spent_usd += cost_usd


async def _already_handled(
    session: AsyncSession, *, asset_id: uuid.UUID, source: str, external_id: str, url: str
) -> bool:
    """True if this item already has an outcome — inserted or suppressed.

    Stops a re-poll of the same feed/GDELT/HN item from spending a second
    LLM call on an article this gate has already decided about.
    """
    in_news = await session.execute(
        sa.text(
            "SELECT 1 FROM asset_news"
            " WHERE asset_id = :asset_id AND source = :source AND external_id = :external_id"
        ),
        {"asset_id": asset_id, "source": source, "external_id": external_id},
    )
    if in_news.first() is not None:
        return True

    in_suppressed = await session.execute(
        sa.text("SELECT 1 FROM news_suppressed WHERE asset_id = :asset_id AND url = :url"),
        {"asset_id": asset_id, "url": url},
    )
    return in_suppressed.first() is not None


async def _insert_suppressed(
    session: AsyncSession, *, asset_id: uuid.UUID, url: str, title: str, score: int, reason: str
) -> None:
    await session.execute(
        sa.text(
            """
            INSERT INTO news_suppressed (asset_id, url, title, score, reason)
            VALUES (:asset_id, :url, :title, :score, :reason)
            ON CONFLICT (asset_id, url) DO NOTHING
            """
        ),
        {"asset_id": asset_id, "url": url, "title": title, "score": score, "reason": reason},
    )


async def prune_old_suppressed(session: AsyncSession) -> int:
    """Delete `news_suppressed` rows older than `SUPPRESSED_RETENTION_DAYS`."""
    result = await session.execute(
        sa.text(
            "DELETE FROM news_suppressed WHERE created_at < now() - (:days * interval '1 day')"
        ),
        {"days": SUPPRESSED_RETENTION_DAYS},
    )
    return result.rowcount or 0


async def _fetch_full_text(url: str) -> str:
    """Download and extract article body text — transient (AUD-658): used
    only as LLM input for this call, never persisted (see docs/news-sources.md).

    Any failure (network, parse, missing `trafilatura`-extractable content)
    degrades to an empty string — classification still proceeds on the
    title alone, same as the heuristic classifier already does.
    """
    import asyncio

    try:
        import trafilatura
    except ImportError:
        return ""

    try:
        downloaded = await asyncio.to_thread(trafilatura.fetch_url, url)
        if not downloaded:
            return ""
        extracted = await asyncio.to_thread(trafilatura.extract, downloaded)
    except Exception as exc:  # noqa: BLE001 -- any extraction failure degrades to title-only, never fails the run
        logger.info("news_gate: full-text fetch failed for %s: %s", url, exc)
        return ""
    return extracted or ""


class NewsAnalysisGate:
    """One instance per `news_refresh` run — owns the run's LLM budget and
    (if configured) its `AnthropicClient`.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        threshold: int,
        client: AnthropicClient | None,
        budget: _RunBudget,
        run_id: uuid.UUID,
    ) -> None:
        self.enabled = enabled
        self._threshold = threshold
        self._client = client
        self._budget = budget
        self._run_id = run_id

    @classmethod
    async def load(cls, session: AsyncSession, *, run_id: uuid.UUID) -> NewsAnalysisGate:
        setting = await get_news_analysis_setting(session)
        if not setting.enabled:
            return cls(
                enabled=False,
                threshold=0,
                client=None,
                budget=_RunBudget(Decimal(0)),
                run_id=run_id,
            )

        settings = get_settings()
        api_key = await get_anthropic_api_key(session)
        client: AnthropicClient | None = None
        if api_key:
            client = AnthropicClient(
                api_key=api_key,
                model=settings.llm_news_model,
                max_output_tokens=settings.llm_news_max_output_tokens,
            )
        else:
            logger.warning(
                "news_gate run_id=%s: gate enabled but no anthropic key configured — "
                "every article fails open unanalyzed",
                run_id,
            )
        return cls(
            enabled=True,
            threshold=setting.importance_threshold,
            client=client,
            budget=_RunBudget(settings.llm_news_budget_usd_per_run),
            run_id=run_id,
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()

    async def ingest(
        self,
        session: AsyncSession,
        *,
        asset_id: uuid.UUID,
        source: str,
        external_id: str,
        title: str,
        url: str,
        news_site: str,
        published_at: datetime,
        asset_symbol: str,
        asset_name: str,
        insert: InsertFn,
        thumbnail_url: str | None = None,
    ) -> int:
        """Insert into `asset_news` (gate off, fail-open, or scored
        important), suppress (scored unimportant), or skip (already
        handled). Returns rows inserted into `asset_news`.
        """
        plain_kwargs = {
            "asset_id": asset_id,
            "source": source,
            "external_id": external_id,
            "title": title,
            "url": url,
            "news_site": news_site,
            "published_at": published_at,
            "thumbnail_url": thumbnail_url,
        }

        if not self.enabled:
            return await insert(session, **plain_kwargs)

        already_handled = await _already_handled(
            session, asset_id=asset_id, source=source, external_id=external_id, url=url
        )
        if already_handled:
            return 0

        if self._client is None or not self._budget.has_room():
            return await insert(session, **plain_kwargs)

        text = await _fetch_full_text(url)
        try:
            result = await self._client.classify_article(
                title=title, text=text, asset_symbol=asset_symbol, asset_name=asset_name
            )
        except LlmUnavailable as exc:
            logger.info("news_gate run_id=%s: llm unavailable, fail-open: %s", self._run_id, exc)
            return await insert(session, **plain_kwargs)

        self._budget.charge(result.cost_usd)

        if result.importance >= self._threshold:
            return await insert(
                session,
                **plain_kwargs,
                summary=result.summary or None,
                importance=result.importance,
            )

        await _insert_suppressed(
            session,
            asset_id=asset_id,
            url=url,
            title=title,
            score=result.importance,
            reason=result.reason or "below importance threshold",
        )
        return 0
