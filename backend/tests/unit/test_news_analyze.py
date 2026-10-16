"""Unit tests for the news-alert heuristic classifier (AUD-583)."""

from __future__ import annotations

import pytest

from audr.jobs.news_analyze import classify_heuristic

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
