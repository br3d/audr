"""Anthropic Messages API adapter — news-analysis gate (ADR-007 phase 3, AUD-658).

A raw httpx call against `/v1/messages`, not the `anthropic` SDK: every other
adapter under `audr.providers` (GDELT, Hacker News, CoinGecko, CoinMarketCap)
already owns its request/parsing directly on `ProviderHttpBase` rather than a
second HTTP dependency, and nothing here needs more than that gives.

Unlike those keyless adapters, every failure here (no key, timeout, 429,
malformed response) *raises* rather than degrading to "no results" — this
adapter has no concept of fail-open. That decision belongs to the caller
(`audr.jobs.news_gate`), which must insert the article unanalyzed rather than
let a provider outage silently stop news collection (AUD-658 acceptance
criteria). Every exception here is an `LlmUnavailable`, so the caller has one
thing to catch.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from decimal import Decimal

import httpx

from audr.providers.http_base import ProviderHttpBase

logger = logging.getLogger(__name__)

_API_URL = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"

# Config defaults (AUD-658: "вынести в конфиг, не хардкодь по месту вызова").
# Model and budget are the founder's call (card 0910f224 on AUD-644) — these
# are only the fallback a fresh install starts from.
DEFAULT_MODEL = "claude-haiku-4-5"
DEFAULT_MAX_OUTPUT_TOKENS = 300
DEFAULT_TIMEOUT = 20.0
DEFAULT_RETRIES = 2
DEFAULT_RETRY_BACKOFF_S = 1.0
DEFAULT_BUDGET_USD_PER_RUN = Decimal("1.00")

# USD per million (input, output) tokens — AUD-658 ticket, priced 2026-10-22.
# Prompt caching is deliberately not modeled: the ticket's own cost analysis
# rules it out (unique article body per call, below both models' minimum
# cacheable prefix).
MODEL_PRICING_PER_MTOK: dict[str, tuple[Decimal, Decimal]] = {
    "claude-haiku-4-5": (Decimal(1), Decimal(5)),
    "claude-sonnet-5": (Decimal(3), Decimal(15)),
    "claude-opus-5": (Decimal(5), Decimal(25)),
}

_SYSTEM_PROMPT = (
    "You triage crypto-asset news for a portfolio owner who already holds "
    "the asset named below. Decide whether the article is significant enough "
    "to show them, and summarise it in your own words — never quote or copy "
    "sentences from the article text.\n\n"
    "Respond with ONLY a JSON object, no prose, no markdown fence:\n"
    '{"important": <bool>, "importance": <int 0-100>, '
    '"summary": "<your own 1-2 sentence summary, or empty if not important>", '
    '"reason": "<short reason for the score>"}\n\n'
    "importance >= 50 means the owner would want to see this about an asset "
    "they hold (price moves, hacks, depegs, delistings, regulatory action, "
    "material protocol news). Routine listicles, generic market commentary "
    "unrelated to this specific asset, and promotional content score low."
)


class LlmUnavailable(Exception):
    """Base for every condition the caller must treat as fail-open."""


class NoApiKey(LlmUnavailable):
    """No Anthropic key is configured, or Anthropic rejected it as invalid."""


class LlmTimeout(LlmUnavailable):
    """The request did not complete within the retry budget."""


class LlmRateLimited(LlmUnavailable):
    """Anthropic returned 429 on every retry."""


class LlmResponseError(LlmUnavailable):
    """2xx response that was not the single JSON object the prompt demands."""


@dataclass(frozen=True)
class ClassificationResult:
    important: bool
    importance: int
    summary: str
    reason: str
    cost_usd: Decimal


def estimate_cost_usd(*, model: str, input_tokens: int, output_tokens: int) -> Decimal:
    """Exact-arithmetic USD cost for a call, from Anthropic's reported usage."""
    input_rate, output_rate = MODEL_PRICING_PER_MTOK.get(
        model, MODEL_PRICING_PER_MTOK[DEFAULT_MODEL]
    )
    million = Decimal(1_000_000)
    return (Decimal(input_tokens) * input_rate + Decimal(output_tokens) * output_rate) / million


def _build_prompt(*, title: str, text: str, asset_symbol: str, asset_name: str) -> str:
    # `text` is the trafilatura-extracted article body (AUD-658: transient —
    # fed to the model, never persisted). Capped generously; Anthropic's own
    # context window is the real backstop, this just keeps an unusually large
    # page from inflating the token bill for no benefit to the classification.
    body = text[:8000] if text else "(full article text unavailable)"
    return f"Held asset: {asset_name} ({asset_symbol})\nHeadline: {title}\n\nArticle text:\n{body}"


def _parse_response(payload: dict, *, model: str) -> ClassificationResult:
    try:
        content = payload["content"][0]["text"]
        usage = payload["usage"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LlmResponseError(f"unexpected response shape: {exc}") from exc

    try:
        decision = json.loads(content)
    except ValueError as exc:
        raise LlmResponseError(f"non-JSON content: {exc}") from exc

    try:
        important = bool(decision["important"])
        importance = max(0, min(100, int(decision["importance"])))
        summary = str(decision.get("summary") or "")
        reason = str(decision.get("reason") or "")
    except (KeyError, TypeError, ValueError) as exc:
        raise LlmResponseError(f"malformed decision JSON: {exc}") from exc

    cost_usd = estimate_cost_usd(
        model=model,
        input_tokens=int(usage.get("input_tokens", 0)),
        output_tokens=int(usage.get("output_tokens", 0)),
    )
    return ClassificationResult(
        important=important,
        importance=importance,
        summary=summary,
        reason=reason,
        cost_usd=cost_usd,
    )


class AnthropicClient:
    """Classifies one article at a time via the Messages API."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        retries: int = DEFAULT_RETRIES,
        retry_backoff_s: float = DEFAULT_RETRY_BACKOFF_S,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        if not api_key:
            raise NoApiKey("no anthropic api key configured")
        self._api_key = api_key
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._retries = retries
        self._retry_backoff_s = retry_backoff_s
        self._http = ProviderHttpBase(http_client=http_client, timeout=timeout)

    @property
    def model(self) -> str:
        return self._model

    async def __aenter__(self) -> AnthropicClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._http.close()

    async def classify_article(
        self, *, title: str, text: str, asset_symbol: str, asset_name: str
    ) -> ClassificationResult:
        """Score one article's significance to the owner for *asset_symbol*.

        Raises an `LlmUnavailable` subclass on any failure — see module
        docstring: this method never degrades silently, the caller decides
        what fail-open means.
        """
        body = {
            "model": self._model,
            "max_tokens": self._max_output_tokens,
            "system": _SYSTEM_PROMPT,
            "messages": [
                {
                    "role": "user",
                    "content": _build_prompt(
                        title=title, text=text, asset_symbol=asset_symbol, asset_name=asset_name
                    ),
                }
            ],
        }
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": _API_VERSION,
            "content-type": "application/json",
        }

        last_exc: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                response = await self._http.client.post(_API_URL, json=body, headers=headers)
            except httpx.TimeoutException as exc:
                last_exc = exc
                if attempt < self._retries:
                    await asyncio.sleep(self._retry_backoff_s * (attempt + 1))
                    continue
                raise LlmTimeout(f"timed out after {attempt + 1} attempt(s)") from exc
            except httpx.HTTPError as exc:
                raise LlmUnavailable(f"request failed: {exc}") from exc

            if response.status_code == 429:
                if attempt < self._retries:
                    await asyncio.sleep(self._retry_backoff_s * (attempt + 1))
                    continue
                raise LlmRateLimited("rate limited on every retry")
            if response.status_code in (401, 403):
                raise NoApiKey(
                    f"anthropic rejected the configured key: HTTP {response.status_code}"
                )
            if not response.is_success:
                raise LlmResponseError(f"HTTP {response.status_code}")

            return _parse_response(response.json(), model=self._model)

        # Unreachable: the loop above always returns or raises.
        raise LlmTimeout("exhausted retries") from last_exc
