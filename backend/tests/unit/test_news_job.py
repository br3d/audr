"""Unit tests for asset-news keyword matching (AUD-308) and release-tier
noise filtering (AUD-640)."""

from __future__ import annotations

import uuid

import pytest

from audr.jobs.news import HeldAsset, is_noisy_release, match_assets, should_drop_feed_item

_WETH = HeldAsset(id=uuid.uuid4(), symbol="WETH", name="Wrapped Ether")
_UNI = HeldAsset(id=uuid.uuid4(), symbol="UNI", name="Uniswap")


@pytest.mark.unit
class TestMatchAssets:
    def test_matches_by_symbol_word_boundary(self) -> None:
        matched = match_assets("WETH surges after upgrade", [_WETH])
        assert matched == [_WETH.id]

    def test_matches_by_name(self) -> None:
        matched = match_assets("Wrapped Ether hits new high", [_WETH])
        assert matched == [_WETH.id]

    def test_case_insensitive(self) -> None:
        matched = match_assets("weth rallies", [_WETH])
        assert matched == [_WETH.id]

    def test_symbol_does_not_match_substring(self) -> None:
        """'UNI' must not match inside an unrelated word like 'university'."""
        matched = match_assets("University announces blockchain course", [_UNI])
        assert matched == []

    def test_no_match_returns_empty(self) -> None:
        matched = match_assets("Bitcoin dominance rises", [_WETH, _UNI])
        assert matched == []

    def test_matches_multiple_assets(self) -> None:
        matched = match_assets("WETH and Uniswap both rally today", [_WETH, _UNI])
        assert set(matched) == {_WETH.id, _UNI.id}

    def test_deduplicates_when_symbol_and_name_both_match(self) -> None:
        """An asset appears once even if both its symbol and name match."""
        matched = match_assets("Uniswap's UNI token gains", [_UNI])
        assert matched == [_UNI.id]


@pytest.mark.unit
class TestIsNoisyRelease:
    """Titles are the real ones observed on staging (AUD-640)."""

    @pytest.mark.parametrize(
        "title",
        [
            "@ensdomains/ensjs@5.2.4",
            "@ensdomains/ensjs-abi@5.0.3",
            "@ensdomains/ensjs-react@5.0.0",
            "viem@2.21.0",
            "v2.10.2-beta5",
            "v2.10.2-preconf (Testing Only)",
            "v1.18.0-rc1",
            "Nightly build 2026-10-19",
            "v3.0.0-alpha.2",
            "v3.0.0-pre-release",
        ],
    )
    def test_noise_is_filtered(self, title: str) -> None:
        assert is_noisy_release(title) is True

    @pytest.mark.parametrize(
        "title",
        [
            "Protein Sludge Pack (v1.17.8)",
            "v2.10.3",
            "v2.10.1-hotfix2",
            "KyberSwap Interface - v7.11.10",
            "ENS Name Wrapper v1.0",
            "Alphanet support lands",
            "Dencun mainnet release",
        ],
    )
    def test_real_releases_survive(self, title: str) -> None:
        assert is_noisy_release(title) is False


@pytest.mark.unit
class TestShouldDropFeedItem:
    """Which feeds the filter applies to (AUD-640 reopened).

    Titles are the real ones on `forum.polygon.technology/c/announcements`,
    MATIC's source since 0038 retired `release:polygon-bor`.
    """

    def test_release_tier_drops_package_tags_and_prereleases(self) -> None:
        for title in ("@ensdomains/ensjs@5.2.4", "v2.10.2-beta5"):
            assert should_drop_feed_item(tier="release", key="release:ensjs", title=title) is True

    def test_release_shaped_project_feed_drops_testnet_builds(self) -> None:
        assert (
            should_drop_feed_item(
                tier="project",
                key="project:polygon-forum-announcements",
                title="Heimdall-v2 v0.12.2-beta-2 (Amoy)",
            )
            is True
        )

    @pytest.mark.parametrize(
        "title",
        [
            "Bor v2.10.3 Release Mainnet and Testnet POS",
            "Security release review – Heimdall v0.12.1 (Lugano HF)",
            "Polygon PoS RPC Incident — Post-Incident Summary",
        ],
    )
    def test_real_announcements_survive_on_that_feed(self, title: str) -> None:
        assert (
            should_drop_feed_item(
                tier="project", key="project:polygon-forum-announcements", title=title
            )
            is False
        )

    def test_other_project_feeds_keep_beta_announcements(self) -> None:
        """A blog post about a beta is news; only release-shaped feeds are filtered."""
        assert (
            should_drop_feed_item(tier="project", key="project:ens-blog", title="Our beta is live")
            is False
        )

    def test_press_tier_is_never_filtered(self) -> None:
        assert (
            should_drop_feed_item(
                tier="press", key="press:coindesk", title="Alpha leak hits markets"
            )
            is False
        )
