"""News-alert heuristic classification job (ADR-007 phase 1, AUD-583/AUD-337).

Walks `asset_news` rows the keyless `news_refresh` job has not yet analyzed
(cursor: `analyzed_at`), classifies each headline with a keyword lexicon, and
persists a `news_alert` row when the result clears the alerting bar
(`severity in {warning, critical}`). LLM classification (ADR-007 phase 3) is
out of scope here — every alert this job writes carries `classifier =
'heuristic'`.

Cooldown dedup is load-bearing, not an optimisation: a single incident (a
protocol hack, say) shows up as a wave of near-identical headlines across
feeds within hours. Without collapsing that wave to one alert per
`(asset_id, category)` per day, the owner would get alert-fatigued into
muting the feature outright — see AUD-583.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from audr.jobs.news import NEWS_RETENTION

logger = logging.getLogger(__name__)

# Caps how many unanalyzed articles one run walks — keeps a large backlog
# (e.g. right after this job ships) from turning one run into an unbounded
# transaction. Remaining rows stay analyzed_at IS NULL and get picked up by
# the next run, same batching idiom as the GDELT tier in jobs/news.py.
_BATCH_LIMIT = 200

# Cooldown window (AUD-583/ADR-007 §2 policy 3): at most one alert per
# (asset_id, category) pair in this window, however many matching headlines
# land within it.
_COOLDOWN = timedelta(hours=24)

_OTHER_CATEGORY = "other"

# Checked in order, most severe first, so a headline matching more than one
# class (a hack article that also mentions a price crash) lands under its
# most urgent category rather than whichever tuple happens to be checked
# last. Each keyword matches on a word boundary — the same discipline
# `audr.jobs.news.match_assets` uses to keep a short keyword like "ban" from
# matching inside "urban"; a multi-word phrase like "rug pull" still works
# since `\b` around the literal space anchors both ends.
_LEXICON: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "hack_exploit",
        "critical",
        (
            "hack",
            "hacked",
            "hacker",
            "exploit",
            "exploited",
            "exploiter",
            "drain",
            "drained",
            "drainer",
            "stolen",
            "steals",
            "rug pull",
            "rugpull",
            "rugged",
            "breach",
            "breached",
            "compromised",
            "vulnerability",
        ),
    ),
    (
        "depeg",
        "critical",
        (
            "depeg",
            "depegs",
            "depegged",
            "depegging",
            "de-peg",
            "unpegged",
            "loses peg",
            "lost its peg",
            "broke peg",
            "breaks peg",
        ),
    ),
    (
        "delisting",
        "critical",
        (
            "delist",
            "delists",
            "delisted",
            "delisting",
            "suspends trading",
            "suspended trading",
            "trading halt",
            "halts trading",
        ),
    ),
    (
        "regulatory",
        "warning",
        (
            "sec sues",
            "sec charges",
            "lawsuit",
            "sued",
            "indicted",
            "indictment",
            "regulatory action",
            "sanctioned",
            "sanctions",
            "subpoena",
            "cftc",
            "doj charges",
            "charged with",
            "class action",
            "banned",
        ),
    ),
    (
        "market_move",
        "warning",
        (
            "plunges",
            "plunged",
            "plunge",
            "crashes",
            "crashed",
            "crash",
            "plummets",
            "plummeted",
            "plummet",
            "tumbles",
            "tumbled",
            "collapses",
            "collapsed",
            "soars",
            "soared",
            "surges",
            "surged",
            "all-time high",
        ),
    ),
)


@dataclass(frozen=True)
class HeuristicResult:
    category: str
    severity: str | None
    reason: str


def classify_heuristic(title: str) -> HeuristicResult:
    """Classify a headline with the keyword lexicon above.

    Returns ``severity=None`` for a headline matching no threat keyword —
    the false-positive-averse default (AUD-583 acceptance criteria): a
    neutral headline must never create an alert.
    """
    for category, severity, keywords in _LEXICON:
        for keyword in keywords:
            if re.search(rf"\b{re.escape(keyword)}\b", title, re.IGNORECASE):
                return HeuristicResult(
                    category=category,
                    severity=severity,
                    reason=f"heuristic match: '{keyword}'",
                )
    return HeuristicResult(
        category=_OTHER_CATEGORY,
        severity=None,
        reason="heuristic: no threat keyword matched",
    )


@dataclass(frozen=True)
class _PendingArticle:
    id: uuid.UUID
    asset_id: uuid.UUID
    title: str


async def _fetch_unanalyzed(session: AsyncSession) -> list[_PendingArticle]:
    rows = await session.execute(
        sa.text(
            """
            SELECT id, asset_id, title
            FROM asset_news
            WHERE analyzed_at IS NULL
            ORDER BY published_at
            LIMIT :limit
            """
        ),
        {"limit": _BATCH_LIMIT},
    )
    return [_PendingArticle(id=row[0], asset_id=row[1], title=row[2]) for row in rows.fetchall()]


async def _in_cooldown(session: AsyncSession, *, asset_id: uuid.UUID, category: str) -> bool:
    row = await session.execute(
        sa.text(
            """
            SELECT 1 FROM news_alert
            WHERE asset_id = :asset_id AND category = :category
              AND created_at > :cutoff
            LIMIT 1
            """
        ),
        {
            "asset_id": asset_id,
            "category": category,
            "cutoff": datetime.now(tz=UTC) - _COOLDOWN,
        },
    )
    return row.first() is not None


async def _mark_analyzed(session: AsyncSession, article_id: uuid.UUID) -> None:
    await session.execute(
        sa.text("UPDATE asset_news SET analyzed_at = now() WHERE id = :id"),
        {"id": article_id},
    )


async def _insert_alert(
    session: AsyncSession,
    *,
    article: _PendingArticle,
    category: str,
    severity: str,
    summary: str,
) -> None:
    await session.execute(
        sa.text(
            """
            INSERT INTO news_alert
              (asset_news_id, asset_id, category, severity, summary, classifier)
            VALUES
              (:asset_news_id, :asset_id, :category, :severity, :summary, 'heuristic')
            """
        ),
        {
            "asset_news_id": article.id,
            "asset_id": article.asset_id,
            "category": category,
            "severity": severity,
            "summary": summary,
        },
    )


async def _prune_old_alerts(session: AsyncSession) -> int:
    """Delete `news_alert` rows older than NEWS_RETENTION.

    Shares `audr.jobs.news.NEWS_RETENTION` (90 days) rather than a second
    constant — ADR-007 §4 calls for one retention number across `asset_news`
    and `news_alert`.
    """
    result = await session.execute(
        sa.text("DELETE FROM news_alert WHERE created_at < :cutoff"),
        {"cutoff": datetime.now(tz=UTC) - NEWS_RETENTION},
    )
    return result.rowcount or 0


async def handle_news_analyze(session: AsyncSession, run_id: uuid.UUID) -> None:
    """Classify unanalyzed `asset_news` rows and create alerts for threats.

    Every row fetched this run is marked analyzed regardless of outcome (no
    match, cooldown-suppressed, or alert created) so a run never reconsiders
    the same headline.
    """
    articles = await _fetch_unanalyzed(session)
    created = 0
    suppressed = 0

    for article in articles:
        result = classify_heuristic(article.title)
        if result.severity is not None:
            if await _in_cooldown(session, asset_id=article.asset_id, category=result.category):
                suppressed += 1
            else:
                await _insert_alert(
                    session,
                    article=article,
                    category=result.category,
                    severity=result.severity,
                    summary=result.reason,
                )
                created += 1
        await _mark_analyzed(session, article.id)

    pruned = await _prune_old_alerts(session)
    await session.commit()
    logger.info(
        "news_analyze run_id=%s scanned=%d created=%d suppressed=%d pruned=%d",
        run_id,
        len(articles),
        created,
        suppressed,
        pruned,
    )
