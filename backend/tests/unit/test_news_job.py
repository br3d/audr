"""Unit tests for asset-news keyword matching (AUD-308), release-tier
noise filtering (AUD-640), and the feed-level keep/block pattern mechanism
(AUD-655)."""

from __future__ import annotations

import re
import uuid

import pytest

from audr.jobs.news import (
    HeldAsset,
    _compile_feed_pattern,
    is_noisy_release,
    match_assets,
    match_assets_strict,
    should_drop_feed_item,
)

_WETH = HeldAsset(id=uuid.uuid4(), symbol="WETH", name="Wrapped Ether")
_UNI = HeldAsset(id=uuid.uuid4(), symbol="UNI", name="Uniswap")
_MATH = HeldAsset(id=uuid.uuid4(), symbol="MATH", name="MATH Wallet")
_ASSET = HeldAsset(id=uuid.uuid4(), symbol="ASSET", name="Asset Token")
_PORTAL = HeldAsset(id=uuid.uuid4(), symbol="PORTAL", name="Portal")
_MIR = HeldAsset(id=uuid.uuid4(), symbol="MIR", name="Mirror Protocol")
_CAKE = HeldAsset(id=uuid.uuid4(), symbol="CAKE", name="PancakeSwap")
_ASSET_REAL = HeldAsset(id=uuid.uuid4(), symbol="ASSET", name="REAL")
_XCN = HeldAsset(id=uuid.uuid4(), symbol="XCN", name="Chain")
_EV = HeldAsset(id=uuid.uuid4(), symbol="EV", name="Everything")


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
class TestMatchAssetsStrict:
    """The aggregator tier's relevance gate (AUD-733): a bare word-boundary
    symbol match is not trusted on its own, because some tickers are
    ordinary English words. Titles are the real ones measured on the stand
    (AUD-733 wake comment)."""

    def test_math_homograph_headline_is_rejected(self) -> None:
        """'MATH' word-boundary-matches an unrelated Hacker News headline
        about mathematics, not the MATH Wallet token."""
        matched = match_assets_strict("Security Rests on Hard Math Problems", [_MATH])
        assert matched == []

    def test_asset_homograph_headline_is_rejected(self) -> None:
        """'ASSET' word-boundary-matches generic crypto-adjacent news that
        never mentions the Asset Token project."""
        matched = match_assets_strict(
            "Can AI Hack Bitcoin? CZ Says the Real Danger Lies in Phishing and Data Leak",
            [_ASSET],
        )
        assert matched == []

    def test_portal_homograph_headline_is_rejected_even_though_name_equals_symbol(self) -> None:
        """PORTAL's project name is itself the bare word 'Portal', so a name
        check gives no extra protection here — this headline is about a
        Coldcard claims portal, not the PORTAL token."""
        matched = match_assets_strict("Portal Opens Claims Check", [_PORTAL])
        assert matched == []

    def test_dollar_ticker_citation_is_accepted(self) -> None:
        matched = match_assets_strict("$MATH surges 12% after exchange listing", [_MATH])
        assert matched == [_MATH.id]

    def test_dollar_ticker_citation_is_case_insensitive(self) -> None:
        matched = match_assets_strict("$math surges 12% after exchange listing", [_MATH])
        assert matched == [_MATH.id]

    def test_paren_ticker_citation_is_accepted(self) -> None:
        """Proves the fix is not just a mute (AUD-733 acceptance criterion
        3): a genuine PORTAL story still alerts when it cites the ticker."""
        matched = match_assets_strict("Portal (PORTAL) Drained in $2M DeFi Exploit", [_PORTAL])
        assert matched == [_PORTAL.id]

    def test_asset_genuine_story_with_ticker_citation_is_accepted(self) -> None:
        matched = match_assets_strict(
            "Asset Token ($ASSET) Suffers Smart Contract Exploit", [_ASSET]
        )
        assert matched == [_ASSET.id]

    def test_distinctive_name_match_is_accepted_without_a_ticker_citation(self) -> None:
        """MIR's genuine true-positive shape: the full project name is
        distinctive enough on its own, so no $/() citation is required."""
        matched = match_assets_strict("Mirror Protocol Exploited", [_MIR])
        assert matched == [_MIR.id]

    def test_non_homograph_ticker_still_matches_by_name(self) -> None:
        """Regression guard: tightening the gate must not break ordinary
        aggregator coverage for a ticker with a genuinely distinctive name."""
        sushi = HeldAsset(id=uuid.uuid4(), symbol="SUSHI", name="SushiSwap")
        matched = match_assets_strict("SushiSwap exploit drains $3M from liquidity pool", [sushi])
        assert matched == [sushi.id]

    def test_cake_pancakeswap_is_the_subject_and_still_matches(self) -> None:
        """CAKE *is* PancakeSwap (AUD-704) — a made-up compound name, not an
        ordinary English word, so it keeps matching by name alone."""
        matched = match_assets_strict(
            "$820K stolen from NEM token's PancakeSwap pool due to contract flaw", [_CAKE]
        )
        assert matched == [_CAKE.id]

    def test_unrelated_headline_matches_nothing(self) -> None:
        matched = match_assets_strict("Bitcoin dominance rises", [_MATH, _ASSET, _PORTAL])
        assert matched == []


@pytest.mark.unit
class TestMatchAssetsStrictNameHomographs:
    """AUD-739: AUD-733 hardened the *symbol* path (`_cites_ticker`) against
    tickers that double as ordinary English words, but left the *name* path
    guarded only by `name != symbol` — which does nothing when the asset's
    name itself is a common word distinct from its symbol. Asset `ASSET`
    (name `REAL`) proved this live on AUD-722: 'CZ Says the Real Danger Lies
    in Phishing' cleared the name path on a bare word-boundary match for
    'REAL', and was only stopped downstream by AUD-735's subject gate."""

    def test_asset_real_homograph_headline_is_rejected(self) -> None:
        """Regression test for AUD-722: the exact production false positive."""
        matched = match_assets_strict("CZ Says the Real Danger Lies in Phishing", [_ASSET_REAL])
        assert matched == []

    def test_xcn_chain_homograph_headline_is_rejected(self) -> None:
        matched = match_assets_strict("Retailers Scramble as Supply Chain Woes Continue", [_XCN])
        assert matched == []

    def test_ev_everything_homograph_headline_is_rejected(self) -> None:
        matched = match_assets_strict("New Plan Promises Everything for One Low Price", [_EV])
        assert matched == []

    def test_common_word_name_still_matches_with_dollar_ticker_citation(self) -> None:
        """Defense in depth must not cost the true positive: a genuine XCN
        story still alerts when it cites the ticker, same as the symbol
        path (AUD-733 acceptance criterion 3)."""
        matched = match_assets_strict("Chain ($XCN) Launches New Mainnet Upgrade", [_XCN])
        assert matched == [_XCN.id]

    def test_common_word_name_still_matches_with_paren_ticker_citation(self) -> None:
        matched = match_assets_strict("Everything (EV) Token Surges After Listing", [_EV])
        assert matched == [_EV.id]


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
    """The release-tier backstop, plus the DB-sourced keep/block patterns
    (AUD-655) that replaced the per-key and per-tier hardcodes.

    Titles are the real ones on `forum.polygon.technology/c/announcements`,
    MATIC's source since 0038 retired `release:polygon-bor`.
    """

    def test_release_tier_backstop_drops_package_tags_and_prereleases(self) -> None:
        """Unconditional, even with no block_pattern configured on the feed —
        this is the one filter AUD-655 kept in code (see should_drop_feed_item)."""
        for title in ("@ensdomains/ensjs@5.2.4", "v2.10.2-beta5"):
            assert should_drop_feed_item(tier="release", key="release:ensjs", title=title) is True

    def test_other_tiers_have_no_backstop(self) -> None:
        """Only `release` gets the unconditional backstop; a project/press/
        aggregator feed with no block_pattern drops nothing."""
        for tier in ("project", "press", "aggregator"):
            assert should_drop_feed_item(tier=tier, key="x", title="v2.10.2-beta5") is False

    def test_block_pattern_drops_matching_titles(self) -> None:
        block_re = re.compile(r"beta|rc", re.IGNORECASE)
        assert (
            should_drop_feed_item(
                tier="project",
                key="project:polygon-forum-announcements",
                title="Heimdall-v2 v0.12.2-beta-2 (Amoy)",
                block_re=block_re,
            )
            is True
        )

    def test_block_pattern_keeps_non_matching_titles(self) -> None:
        block_re = re.compile(r"beta|rc", re.IGNORECASE)
        assert (
            should_drop_feed_item(
                tier="project",
                key="project:polygon-forum-announcements",
                title="Bor v2.10.3 Release Mainnet and Testnet POS",
                block_re=block_re,
            )
            is False
        )

    def test_keep_pattern_drops_non_matching_titles(self) -> None:
        keep_re = re.compile(r"exploit|hack", re.IGNORECASE)
        assert (
            should_drop_feed_item(
                tier="aggregator",
                key="aggregator:sushi",
                title="What Is SushiSwap and How Does It Work? — Binance",
                keep_re=keep_re,
            )
            is True
        )

    def test_keep_pattern_keeps_matching_titles(self) -> None:
        keep_re = re.compile(r"exploit|hack", re.IGNORECASE)
        assert (
            should_drop_feed_item(
                tier="aggregator",
                key="aggregator:sushi",
                title="SushiSwap exploit drains $3M from liquidity pool",
                keep_re=keep_re,
            )
            is False
        )

    def test_keep_then_block_a_title_matching_keep_can_still_be_blocked(self) -> None:
        """Order is keep, then block (AUD-655) — passing the allow-list does
        not exempt a title from the block list."""
        keep_re = re.compile(r"sushiswap", re.IGNORECASE)
        block_re = re.compile(r"price prediction", re.IGNORECASE)
        assert (
            should_drop_feed_item(
                tier="aggregator",
                key="aggregator:sushi",
                title="SushiSwap (SUSHI) Price Prediction 2026-2030",
                keep_re=keep_re,
                block_re=block_re,
            )
            is True
        )

    def test_keep_then_block_a_title_failing_keep_is_dropped_before_block_is_checked(
        self,
    ) -> None:
        keep_re = re.compile(r"exploit", re.IGNORECASE)
        block_re = re.compile(r"unmatchable-pattern")
        assert (
            should_drop_feed_item(
                tier="aggregator",
                key="aggregator:sushi",
                title="Binance lists new SushiSwap trading pair",
                keep_re=keep_re,
                block_re=block_re,
            )
            is True
        )

    def test_press_tier_with_no_patterns_is_never_filtered(self) -> None:
        assert (
            should_drop_feed_item(
                tier="press", key="press:coindesk", title="Alpha leak hits markets"
            )
            is False
        )


@pytest.mark.unit
class TestCompileFeedPattern:
    """Compiling a feed's keep/block pattern must tolerate a broken regex
    stored in the DB (AUD-655 req. 4) — a feed must keep polling without the
    rule rather than fail the whole run."""

    def test_none_pattern_compiles_to_none_with_no_error(self) -> None:
        compiled, had_error = _compile_feed_pattern("aggregator:sushi", "block", None)
        assert compiled is None
        assert had_error is False

    def test_valid_pattern_compiles(self) -> None:
        compiled, had_error = _compile_feed_pattern("aggregator:sushi", "block", r"beta|rc")
        assert had_error is False
        assert compiled is not None
        assert compiled.search("v2.10.2-beta5")

    def test_valid_pattern_is_case_insensitive(self) -> None:
        compiled, _had_error = _compile_feed_pattern("aggregator:sushi", "block", r"beta")
        assert compiled is not None
        assert compiled.search("BETA RELEASE")

    def test_broken_pattern_does_not_raise_and_reports_the_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level("WARNING")
        compiled, had_error = _compile_feed_pattern("aggregator:sushi", "block", r"(unclosed")
        assert compiled is None
        assert had_error is True
        assert "aggregator:sushi" in caplog.text
        assert "block" in caplog.text
