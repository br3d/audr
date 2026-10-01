"""Unit tests for retry policy and shared RPC rate limiting (AUD-362)."""

from __future__ import annotations

import time

import pytest

from audr.jobs.policy import (
    RateLimiter,
    RetryPolicy,
    get_shared_cmc_rate_limiter,
    get_shared_rpc_rate_limiter,
)


@pytest.mark.unit
class TestRetryPolicy:
    def test_is_retryable_within_max_attempts(self) -> None:
        policy = RetryPolicy(max_attempts=3)
        assert policy.is_retryable(0) is True
        assert policy.is_retryable(2) is True
        assert policy.is_retryable(3) is False

    def test_delay_for_grows_exponentially_without_jitter(self) -> None:
        policy = RetryPolicy(base_delay_s=1.0, max_delay_s=60.0, jitter=False)
        assert policy.delay_for(0) == 1.0
        assert policy.delay_for(1) == 2.0
        assert policy.delay_for(2) == 4.0

    def test_delay_for_caps_at_max_delay(self) -> None:
        policy = RetryPolicy(base_delay_s=1.0, max_delay_s=5.0, jitter=False)
        assert policy.delay_for(10) == 5.0


@pytest.mark.unit
class TestRateLimiter:
    def test_rejects_non_positive_rate(self) -> None:
        with pytest.raises(ValueError):
            RateLimiter(calls_per_second=0)

    async def test_acquire_within_burst_does_not_block(self) -> None:
        limiter = RateLimiter(calls_per_second=10.0, burst=3)
        start = time.monotonic()
        for _ in range(3):
            await limiter.acquire()
        elapsed = time.monotonic() - start
        assert elapsed < 0.2

    async def test_acquire_beyond_burst_waits(self) -> None:
        limiter = RateLimiter(calls_per_second=20.0, burst=1)
        await limiter.acquire()  # consume the only burst token
        start = time.monotonic()
        await limiter.acquire()
        elapsed = time.monotonic() - start
        # calls_per_second=20 => ~0.05s between tokens
        assert elapsed >= 0.03


@pytest.mark.unit
class TestSharedRpcRateLimiter:
    def test_returns_same_instance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost/audr")
        monkeypatch.setenv("SECRET_KEY", "super-secret-key")
        get_shared_rpc_rate_limiter.cache_clear()
        try:
            first = get_shared_rpc_rate_limiter()
            second = get_shared_rpc_rate_limiter()
            assert first is second
        finally:
            get_shared_rpc_rate_limiter.cache_clear()

    def test_configured_from_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost/audr")
        monkeypatch.setenv("SECRET_KEY", "super-secret-key")
        monkeypatch.setenv("RPC_RATE_LIMIT_PER_SECOND", "2")
        monkeypatch.setenv("RPC_RATE_LIMIT_BURST", "1")
        from audr.config import get_settings

        get_settings.cache_clear()
        get_shared_rpc_rate_limiter.cache_clear()
        try:
            limiter = get_shared_rpc_rate_limiter()
            assert limiter._interval == pytest.approx(0.5)
            assert limiter._burst == 1
        finally:
            get_settings.cache_clear()
            get_shared_rpc_rate_limiter.cache_clear()


@pytest.mark.unit
class TestSharedCmcRateLimiter:
    def test_returns_same_instance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost/audr")
        monkeypatch.setenv("SECRET_KEY", "super-secret-key")
        get_shared_cmc_rate_limiter.cache_clear()
        try:
            first = get_shared_cmc_rate_limiter()
            second = get_shared_cmc_rate_limiter()
            assert first is second
        finally:
            get_shared_cmc_rate_limiter.cache_clear()

    def test_configured_from_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost/audr")
        monkeypatch.setenv("SECRET_KEY", "super-secret-key")
        monkeypatch.setenv("CMC_RATE_LIMIT_PER_SECOND", "0.25")
        monkeypatch.setenv("CMC_RATE_LIMIT_BURST", "1")
        from audr.config import get_settings

        get_settings.cache_clear()
        get_shared_cmc_rate_limiter.cache_clear()
        try:
            limiter = get_shared_cmc_rate_limiter()
            assert limiter._interval == pytest.approx(4.0)
            assert limiter._burst == 1
        finally:
            get_settings.cache_clear()
            get_shared_cmc_rate_limiter.cache_clear()

    def test_defaults_are_conservative(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Default budget is far tighter than the RPC default (AUD-370)."""
        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost/audr")
        monkeypatch.setenv("SECRET_KEY", "super-secret-key")
        from audr.config import get_settings

        get_settings.cache_clear()
        get_shared_cmc_rate_limiter.cache_clear()
        try:
            limiter = get_shared_cmc_rate_limiter()
            assert limiter._interval == pytest.approx(2.0)  # 0.5 calls/s
            assert limiter._burst == 1
        finally:
            get_settings.cache_clear()
            get_shared_cmc_rate_limiter.cache_clear()
