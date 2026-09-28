"""Retry policy, shared RPC rate limiting, and cancellation checkpoints (T019)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class RetryPolicy:
    """Bounded exponential backoff with jitter."""

    max_attempts: int = 3
    base_delay_s: float = 1.0
    max_delay_s: float = 60.0
    jitter: bool = True

    def delay_for(self, attempt: int) -> float:
        """Return the sleep duration (seconds) for the given attempt number (0-indexed)."""
        exp = min(self.base_delay_s * (2**attempt), self.max_delay_s)
        if self.jitter:
            import random  # noqa: S311 — non-crypto jitter, entropy not required

            exp = random.uniform(exp * 0.5, exp)  # noqa: S311
        return exp

    def is_retryable(self, attempt: int) -> bool:
        return attempt < self.max_attempts


class RateLimiter:
    """Token-bucket rate limiter for shared RPC request budgets.

    Concurrency-safe via a single asyncio.Semaphore — suitable for use in a
    single-process async worker.
    """

    def __init__(self, *, calls_per_second: float, burst: int = 1) -> None:
        if calls_per_second <= 0:
            raise ValueError("calls_per_second must be positive")
        self._interval = 1.0 / calls_per_second
        self._burst = burst
        self._tokens = float(burst)
        self._lock = asyncio.Lock()
        self._last_refill: float | None = None

    async def acquire(self) -> None:
        """Block until a token is available."""
        async with self._lock:
            now = asyncio.get_event_loop().time()
            if self._last_refill is not None:
                elapsed = now - self._last_refill
                self._tokens = min(
                    float(self._burst),
                    self._tokens + elapsed / self._interval,
                )
            self._last_refill = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            wait = (1.0 - self._tokens) * self._interval
        await asyncio.sleep(wait)
        async with self._lock:
            self._tokens = max(0.0, self._tokens - 1.0)


class CancellationCheckpoint:
    """Async context manager that raises asyncio.CancelledError at safe points."""

    def __init__(self, stop_event: asyncio.Event) -> None:
        self._stop = stop_event

    async def check(self) -> None:
        """Yield to the event loop and raise if cancellation was requested."""
        await asyncio.sleep(0)
        if self._stop.is_set():
            raise asyncio.CancelledError("cancellation requested at checkpoint")
