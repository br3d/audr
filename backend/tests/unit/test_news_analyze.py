"""Unit tests for the news-alert heuristic classifier (AUD-583)."""

from __future__ import annotations

import pytest

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
