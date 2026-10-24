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
muting the feature outright — see AUD-583. The same wave also arrives
sideways: one story naming five held tokens matches five assets and so
clears five separate `(asset_id, category)` cooldowns, which is why there
is a second, story-level dedup on the normalised headline (AUD-683).
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

# Recency gate (AUD-732): an article older than this is never alerted on,
# however it classifies — only marked analyzed. The aggregator feed tier's
# first poll ingests a feed's full history (migration 0039), so
# `analyzed_at IS NULL` alone cannot tell a same-day story from two months of
# backfill. 72h is wider than `_COOLDOWN` on purpose: a feed can lag its own
# `published_at` by a day or more, and the goal here is only to reject a
# backfill-shaped age gap, not to shorten the window the owner already reads
# alerts as "current" within.
_RECENCY_WINDOW = timedelta(hours=72)

_OTHER_CATEGORY = "other"

# Feed titles carry a publisher suffix (" - CoinDesk", " - Yahoo Finance"), so
# the same wire story reaches us under several spellings. Stripped before the
# story-level dedup below compares two headlines.
_PUBLISHER_SUFFIX = re.compile(r"\s+[-|–—]\s+[^-|–—]{1,40}$")
_NON_WORD = re.compile(r"[^a-z0-9]+")


def _story_key(title: str) -> str:
    """Normalise a headline to a comparison key for story-level dedup.

    Drops the publisher suffix, lowercases, and collapses punctuation —
    enough to collapse the same story syndicated across feeds without
    collapsing two genuinely different headlines about one asset.
    """
    return _NON_WORD.sub(" ", _PUBLISHER_SUFFIX.sub("", title).lower()).strip()


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


# Attribution gate (AUD-735): a headline can name an asset as the mere
# *instrument* an event passed through (funds moved "in Ethereum", a rival
# token named as delisting's beneficiary) rather than as the thing the event
# happened to. The cheap heuristic cannot parse grammar, so it falls back to
# a proximity signal: a word that is genuinely the event's subject is named
# within a few words of the matched keyword ("Mirror Protocol Exploited",
# "NEM token's PancakeSwap pool" + "stolen"); an incidental mention sits
# further away, past a second verb or relational clause ("Sends $61 Million
# in Ethereum to Tornado Cash", "Propel Rival cbBTC"). Measured on the four
# fixtures this gate exists to fix: true subjects sit at a 1- and 3-word
# gap, instrument-only mentions at a 4-word gap — this threshold sits
# between them.
_PROXIMITY_WINDOW = 3

_TOKEN_RE = re.compile(r"\S+")


def _token_index_at(spans: list[tuple[int, int]], char_pos: int) -> int:
    for index, (start, end) in enumerate(spans):
        if start <= char_pos < end:
            return index
    return len(spans) - 1


def _is_near_event(title: str, keyword_match: re.Match[str], asset_terms: tuple[str, ...]) -> bool:
    """True if any asset term sits within `_PROXIMITY_WINDOW` words of the keyword.

    Fails open (returns True) when none of `asset_terms` can be located in
    the title — this gate only narrows an existing match, so it must never
    be the reason a real match goes unmatched because of a lookup gap
    upstream.
    """
    spans = [m.span() for m in _TOKEN_RE.finditer(title)]
    keyword_index = _token_index_at(spans, keyword_match.start())
    found = False
    for term in asset_terms:
        if not term:
            continue
        asset_match = re.search(rf"\b{re.escape(term)}\b", title, re.IGNORECASE)
        if asset_match is None:
            continue
        found = True
        asset_index = _token_index_at(spans, asset_match.start())
        gap = abs(asset_index - keyword_index) - 1
        if gap <= _PROXIMITY_WINDOW:
            return True
    return not found


def classify_heuristic(
    title: str, *, asset_symbol: str | None = None, asset_name: str | None = None
) -> HeuristicResult:
    """Classify a headline with the keyword lexicon above.

    Returns ``severity=None`` for a headline matching no threat keyword —
    the false-positive-averse default (AUD-583 acceptance criteria): a
    neutral headline must never create an alert.

    When `asset_symbol`/`asset_name` are given, a keyword match is also
    gated on `_is_near_event` (AUD-735): the asset must be the event's
    grammatical subject, not merely named elsewhere in the headline as the
    instrument it passed through or a beneficiary it names.
    """
    for category, severity, keywords in _LEXICON:
        for keyword in keywords:
            match = re.search(rf"\b{re.escape(keyword)}\b", title, re.IGNORECASE)
            if match is None:
                continue
            if not _is_near_event(title, match, (asset_symbol or "", asset_name or "")):
                return HeuristicResult(
                    category=category,
                    severity=None,
                    reason=(
                        f"heuristic: '{keyword}' matched but asset is not its "
                        "subject (word gap exceeds proximity window)"
                    ),
                )
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
    published_at: datetime
    asset_symbol: str = ""
    asset_name: str = ""


async def _fetch_unanalyzed(session: AsyncSession) -> list[_PendingArticle]:
    """Fetch the next batch of unanalyzed articles, newest first.

    Newest-first (AUD-732) matters once a backlog exists: a large backfill
    (an aggregator feed's first poll, or this job catching up after
    downtime) must not push today's news to the back of a FIFO queue behind
    weeks of old headlines it is about to discard anyway via the recency
    gate below.
    """
    rows = await session.execute(
        sa.text(
            """
            SELECT an.id, an.asset_id, an.title, an.published_at, a.symbol, a.name
            FROM asset_news an
            JOIN asset a ON a.id = an.asset_id
            WHERE an.analyzed_at IS NULL
            ORDER BY an.published_at DESC
            LIMIT :limit
            """
        ),
        {"limit": _BATCH_LIMIT},
    )
    return [
        _PendingArticle(
            id=row[0],
            asset_id=row[1],
            title=row[2],
            published_at=row[3],
            asset_symbol=row[4],
            asset_name=row[5],
        )
        for row in rows.fetchall()
    ]


def _is_stale(published_at: datetime, *, now: datetime) -> bool:
    """True once an article falls outside `_RECENCY_WINDOW` of `now`.

    A stale article is still classified and marked analyzed (so it never
    re-queues) — this predicate only gates whether `handle_news_analyze`
    creates an alert for it.
    """
    return published_at < now - _RECENCY_WINDOW


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


async def _recent_story_keys(session: AsyncSession) -> set[str]:
    """Normalised headlines already alerted on inside the cooldown window.

    The `(asset_id, category)` cooldown cannot see the other half of the
    duplicate problem (AUD-683): one story that mentions five held tokens
    matches five different assets, clears five separate cooldowns, and lands
    as five alerts the owner reads as the same news repeated.
    """
    rows = await session.execute(
        sa.text(
            """
            SELECT an.title
            FROM news_alert na
            JOIN asset_news an ON an.id = na.asset_news_id
            WHERE na.created_at > :cutoff
            """
        ),
        {"cutoff": datetime.now(tz=UTC) - _COOLDOWN},
    )
    return {_story_key(row[0]) for row in rows.fetchall() if row[0]}


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
    now = datetime.now(UTC)
    created = 0
    suppressed = 0
    stale = 0
    story_keys = await _recent_story_keys(session)

    for article in articles:
        result = classify_heuristic(
            article.title, asset_symbol=article.asset_symbol, asset_name=article.asset_name
        )
        if result.severity is not None:
            if _is_stale(article.published_at, now=now):
                stale += 1
            else:
                story_key = _story_key(article.title)
                if story_key in story_keys or await _in_cooldown(
                    session, asset_id=article.asset_id, category=result.category
                ):
                    suppressed += 1
                else:
                    story_keys.add(story_key)
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
        "news_analyze run_id=%s scanned=%d created=%d suppressed=%d stale=%d pruned=%d",
        run_id,
        len(articles),
        created,
        suppressed,
        stale,
        pruned,
    )
