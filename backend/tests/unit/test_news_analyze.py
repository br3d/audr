"""Unit tests for the news-alert heuristic classifier (AUD-583)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from audr.jobs import news_analyze as mod
from audr.jobs.news_analyze import _story_key, classify_heuristic

pytestmark = pytest.mark.unit


class TestClassifyHeuristic:
    def test_hack_exploit_is_critical(self) -> None:
        result = classify_heuristic("Protocol X hacked for $10M in flash loan exploit")
        assert result.category == "hack_exploit"
        assert result.severity == "critical"

    def test_depeg_is_critical(self) -> None:
        result = classify_heuristic("Stablecoin Y depegs after liquidity crunch")
        assert result.category == "depeg"
        assert result.severity == "critical"

    def test_delisting_is_critical(self) -> None:
        result = classify_heuristic("Exchange Z announces token delisting next week")
        assert result.category == "delisting"
        assert result.severity == "critical"

    def test_regulatory_is_warning(self) -> None:
        result = classify_heuristic("SEC charges founder over unregistered securities")
        assert result.category == "regulatory"
        assert result.severity == "warning"

    def test_market_move_is_warning(self) -> None:
        result = classify_heuristic("Token price crashes 40% amid market selloff")
        assert result.category == "market_move"
        assert result.severity == "warning"

    def test_neutral_headline_has_no_severity(self) -> None:
        """A neutral headline must never create an alert (AUD-583 accept. criteria)."""
        result = classify_heuristic("Project announces quarterly developer update")
        assert result.severity is None
        assert result.category == "other"

    def test_case_insensitive(self) -> None:
        result = classify_heuristic("protocol HACKED overnight")
        assert result.category == "hack_exploit"

    def test_word_boundary_does_not_match_substring(self) -> None:
        """'ban' inside 'urban' must not trigger the regulatory category."""
        result = classify_heuristic("Urban development fund adds crypto exposure")
        assert result.severity is None

    def test_most_severe_category_wins_on_multi_match(self) -> None:
        """A headline matching both hack_exploit and market_move keywords is
        classified under the earlier (more severe) category."""
        result = classify_heuristic("Hacked protocol's token crashes after exploit")
        assert result.category == "hack_exploit"
        assert result.severity == "critical"

    def test_reason_names_the_matched_keyword(self) -> None:
        result = classify_heuristic("Bridge exploited for millions")
        assert "exploited" in result.reason


class TestStoryKey:
    """Story-level dedup (AUD-683): one wire story reaches us spelled several
    ways — per-asset copies and per-publisher suffixes — and must collapse to
    one key, without collapsing two genuinely different headlines."""

    def test_publisher_suffix_is_stripped(self) -> None:
        assert _story_key("Bridge exploited for $40M - CoinDesk") == _story_key(
            "Bridge exploited for $40M - Decrypt"
        )

    def test_punctuation_and_case_do_not_split_a_story(self) -> None:
        assert _story_key("Bridge Exploited, $40M Drained") == _story_key(
            "bridge exploited $40m drained"
        )

    def test_different_headlines_keep_different_keys(self) -> None:
        assert _story_key("Bridge exploited for $40M - CoinDesk") != _story_key(
            "Exchange halts trading after outage - CoinDesk"
        )

    def test_a_long_tail_is_not_mistaken_for_a_publisher(self) -> None:
        """The suffix rule only eats a short trailing segment — a dash inside a
        headline must not swallow half of it."""
        key = _story_key("Protocol hacked - and the team says the treasury is still fully solvent")
        assert "treasury" in key


class TestIsStale:
    """Recency gate (AUD-732): an article older than `_RECENCY_WINDOW` relative
    to `now` is stale, regardless of how it classifies."""

    def test_article_inside_window_is_not_stale(self) -> None:
        now = datetime(2026, 10, 24, tzinfo=UTC)
        published_at = now - mod._RECENCY_WINDOW + timedelta(minutes=1)
        assert mod._is_stale(published_at, now=now) is False

    def test_article_outside_window_is_stale(self) -> None:
        now = datetime(2026, 10, 24, tzinfo=UTC)
        published_at = now - mod._RECENCY_WINDOW - timedelta(minutes=1)
        assert mod._is_stale(published_at, now=now) is True

    def test_exactly_at_the_boundary_is_not_stale(self) -> None:
        now = datetime(2026, 10, 24, tzinfo=UTC)
        published_at = now - mod._RECENCY_WINDOW
        assert mod._is_stale(published_at, now=now) is False

    def test_a_two_month_old_backfill_article_is_stale(self) -> None:
        """The exact shape AUD-732 reported: an aggregator feed's first poll
        surfacing an article published two months before the dispatch run."""
        now = datetime(2026, 10, 22, tzinfo=UTC)
        published_at = datetime(2026, 8, 22, tzinfo=UTC)
        assert mod._is_stale(published_at, now=now) is True


class TestFetchUnanalyzedOrdering:
    """AUD-732: a large backfill must not starve today's news out of the
    batch, so the query walks newest-first."""

    async def test_query_orders_by_published_at_descending(self) -> None:
        result = MagicMock()
        result.fetchall.return_value = []
        session = MagicMock()
        session.execute = AsyncMock(return_value=result)

        await mod._fetch_unanalyzed(session)

        query = session.execute.call_args.args[0]
        assert "ORDER BY published_at DESC" in str(query)


class TestHandleNewsAnalyzeRecencyGate:
    """Orchestration-level coverage of the recency gate: a fresh article
    alerts, a stale one does not — but both get marked analyzed so neither
    re-queues forever."""

    async def test_fresh_alerts_stale_is_suppressed_but_both_marked_analyzed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        fresh = mod._PendingArticle(
            id=uuid.uuid4(),
            asset_id=uuid.uuid4(),
            title="Protocol hacked for $1M in flash loan exploit",
            published_at=now,
        )
        stale = mod._PendingArticle(
            id=uuid.uuid4(),
            asset_id=uuid.uuid4(),
            title="Bridge drained of $2M by attacker",
            published_at=now - mod._RECENCY_WINDOW - timedelta(days=60),
        )

        monkeypatch.setattr(mod, "_fetch_unanalyzed", AsyncMock(return_value=[fresh, stale]))
        monkeypatch.setattr(mod, "_recent_story_keys", AsyncMock(return_value=set()))
        monkeypatch.setattr(mod, "_in_cooldown", AsyncMock(return_value=False))
        monkeypatch.setattr(mod, "_prune_old_alerts", AsyncMock(return_value=0))
        insert_alert = AsyncMock()
        monkeypatch.setattr(mod, "_insert_alert", insert_alert)
        mark_analyzed = AsyncMock()
        monkeypatch.setattr(mod, "_mark_analyzed", mark_analyzed)

        session = AsyncMock()
        await mod.handle_news_analyze(session, uuid.uuid4())

        insert_alert.assert_awaited_once()
        assert insert_alert.await_args.kwargs["article"].id == fresh.id

        marked_ids = {call.args[1] for call in mark_analyzed.await_args_list}
        assert marked_ids == {fresh.id, stale.id}
