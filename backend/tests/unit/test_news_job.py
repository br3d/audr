"""Unit tests for asset-news keyword matching (AUD-308) and release-tier
noise filtering (AUD-640)."""

from __future__ import annotations

import uuid

import pytest

from audr.jobs.news import HeldAsset, is_noisy_release, match_assets

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
