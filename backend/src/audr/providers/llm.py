"""LLM adapters — news-analysis gate (ADR-007 phase 3, AUD-658, AUD-932).

Two implementations of one `LlmClient` contract: `AnthropicClient` (native
Messages API) and `OpenAICompatibleClient` (`POST {base_url}/chat/completions`,
which covers OpenAI, OpenRouter, Groq, DeepSeek, Mistral, Gemini-compat, Ollama,
LM Studio ...).

Original AUD-658 notes on the Anthropic adapter:

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
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol, Self

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


Prices = tuple[Decimal, Decimal]


def resolve_prices(*, model: str, provider: str, override: Prices | None = None) -> Prices:
    """USD per Mtok (input, output) for a model.

    An owner-stored price wins. Otherwise the Claude table applies to the
    Anthropic provider only (unknown Claude model → the default model's price,
    the conservative reading); an OpenAI-compatible model has no known price and
    costs $0 — the per-run article cap, not the USD ceiling, bounds those.
    """
    if override is not None:
        return override
    if provider == "anthropic":
        return MODEL_PRICING_PER_MTOK.get(model, MODEL_PRICING_PER_MTOK[DEFAULT_MODEL])
    return (Decimal(0), Decimal(0))


def estimate_cost_usd(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    prices: Prices | None = None,
    provider: str = "anthropic",
) -> Decimal:
    """Exact-arithmetic USD cost for a call, from the provider's reported usage."""
    input_rate, output_rate = resolve_prices(model=model, provider=provider, override=prices)
    million = Decimal(1_000_000)
    return (Decimal(input_tokens) * input_rate + Decimal(output_tokens) * output_rate) / million


def _build_prompt(*, title: str, text: str, asset_symbol: str, asset_name: str) -> str:
    # `text` is the trafilatura-extracted article body (AUD-658: transient —
    # fed to the model, never persisted). Capped generously; Anthropic's own
    # context window is the real backstop, this just keeps an unusually large
    # page from inflating the token bill for no benefit to the classification.
    body = text[:8000] if text else "(full article text unavailable)"
    return f"Held asset: {asset_name} ({asset_symbol})\nHeadline: {title}\n\nArticle text:\n{body}"


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _extract_json_object(content: str) -> dict[str, Any]:
    """Parse the decision object, tolerating a markdown fence or surrounding prose.

    Local and third-party models routinely ignore "ONLY a JSON object".
    """
    candidates = [content.strip()]
    fenced = _FENCE_RE.search(content)
    if fenced:
        candidates.append(fenced.group(1).strip())
    start, end = content.find("{"), content.rfind("}")
    if start != -1 and end > start:
        candidates.append(content[start : end + 1])
    last: Exception | None = None
    for candidate in candidates:
        try:
            decoded = json.loads(candidate)
        except ValueError as exc:
            last = exc
            continue
        if isinstance(decoded, dict):
            return decoded
    raise LlmResponseError(f"non-JSON content: {last}")


def _decision_to_result(
    content: str, *, input_tokens: int, output_tokens: int, cost_usd: Decimal
) -> ClassificationResult:
    decision = _extract_json_object(content)
    try:
        important = bool(decision["important"])
        importance = max(0, min(100, int(decision["importance"])))
        summary = str(decision.get("summary") or "")
        reason = str(decision.get("reason") or "")
    except (KeyError, TypeError, ValueError) as exc:
        raise LlmResponseError(f"malformed decision JSON: {exc}") from exc
    return ClassificationResult(
        important=important,
        importance=importance,
        summary=summary,
        reason=reason,
        cost_usd=cost_usd,
    )


class LlmClient(Protocol):
    """What the news gate and the validate job need from any provider."""

    @property
    def model(self) -> str: ...

    async def classify_article(
        self, *, title: str, text: str, asset_symbol: str, asset_name: str
    ) -> ClassificationResult: ...

    async def check_connectivity(self) -> None: ...

    async def close(self) -> None: ...


class _HttpLlmClient:
    """Shared retry/error mapping; subclasses supply URL, headers, body, parsing."""

    _provider = ""
    _label = ""

    def __init__(
        self,
        *,
        model: str,
        max_output_tokens: int,
        retries: int,
        retry_backoff_s: float,
        http_client: httpx.AsyncClient | None,
        timeout: float,
        prices: Prices | None,
    ) -> None:
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._retries = retries
        self._retry_backoff_s = retry_backoff_s
        self._prices = prices
        self._http = ProviderHttpBase(http_client=http_client, timeout=timeout)

    @property
    def model(self) -> str:
        return self._model

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._http.close()

    def _cost(self, input_tokens: int, output_tokens: int) -> Decimal:
        return estimate_cost_usd(
            model=self._model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            prices=self._prices,
            provider=self._provider,
        )

    # -- subclass hooks ---------------------------------------------------
    def _url(self) -> str:
        raise NotImplementedError

    def _headers(self) -> dict[str, str]:
        raise NotImplementedError

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST with this client's retry and error mapping."""
        headers = self._headers()
        url = self._url()
        last_exc: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                response = await self._http.client.post(url, json=body, headers=headers)
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
                    f"{self._label} rejected the configured key: HTTP {response.status_code}"
                )
            if not response.is_success:
                raise LlmResponseError(f"HTTP {response.status_code}")

            try:
                payload = response.json()
            except ValueError as exc:
                raise LlmResponseError(f"non-JSON response body: {exc}") from exc
            if not isinstance(payload, dict):
                raise LlmResponseError("response body was not a JSON object")
            return payload

        # Unreachable: the loop above always returns or raises.
        raise LlmTimeout("exhausted retries") from last_exc


class AnthropicClient(_HttpLlmClient):
    """Classifies one article at a time via the Anthropic Messages API."""

    _provider = "anthropic"
    _label = "anthropic"

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
        prices: Prices | None = None,
    ) -> None:
        if not api_key:
            raise NoApiKey("no anthropic api key configured")
        super().__init__(
            model=model,
            max_output_tokens=max_output_tokens,
            retries=retries,
            retry_backoff_s=retry_backoff_s,
            http_client=http_client,
            timeout=timeout,
            prices=prices,
        )
        self._api_key = api_key

    def _url(self) -> str:
        return _API_URL

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": _API_VERSION,
            "content-type": "application/json",
        }

    async def classify_article(
        self, *, title: str, text: str, asset_symbol: str, asset_name: str
    ) -> ClassificationResult:
        """Score one article's significance to the owner for *asset_symbol*.

        Raises an `LlmUnavailable` subclass on any failure — this method never
        degrades silently, the caller decides what fail-open means.
        """
        payload = await self._post(
            {
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
        )
        try:
            content = payload["content"][0]["text"]
            usage = payload["usage"]
            in_tok, out_tok = int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
            raise LlmResponseError(f"unexpected response shape: {exc}") from exc
        return _decision_to_result(
            content,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=self._cost(in_tok, out_tok),
        )

    async def check_connectivity(self) -> None:
        """Probe the stored key with the smallest possible Messages call (AUD-743).

        Proves that *this* key reaches *this* model. Returns None on success;
        raises an `LlmUnavailable` subclass otherwise.
        """
        await self._post(
            {
                "model": self._model,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}],
            }
        )


class OpenAICompatibleClient(_HttpLlmClient):
    """Classifies via `POST {base_url}/chat/completions` (OpenAI wire format).

    The API key is optional — a local Ollama / LM Studio takes none. When set it
    goes out as `Authorization: Bearer`.
    """

    _provider = "openai_compatible"
    _label = "provider"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        retries: int = DEFAULT_RETRIES,
        retry_backoff_s: float = DEFAULT_RETRY_BACKOFF_S,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        prices: Prices | None = None,
    ) -> None:
        if not base_url:
            raise LlmUnavailable("no base url configured")
        if not model:
            raise LlmUnavailable("no model configured")
        super().__init__(
            model=model,
            max_output_tokens=max_output_tokens,
            retries=retries,
            retry_backoff_s=retry_backoff_s,
            http_client=http_client,
            timeout=timeout,
            prices=prices,
        )
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key or None

    def _url(self) -> str:
        return f"{self._base_url}/chat/completions"

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self._api_key:
            headers["authorization"] = f"Bearer {self._api_key}"
        return headers

    async def classify_article(
        self, *, title: str, text: str, asset_symbol: str, asset_name: str
    ) -> ClassificationResult:
        payload = await self._post(
            {
                "model": self._model,
                "max_tokens": self._max_output_tokens,
                # Servers that do not support it ignore the field; the parser
                # tolerates fenced/prose-wrapped output either way.
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": _build_prompt(
                            title=title, text=text, asset_symbol=asset_symbol, asset_name=asset_name
                        ),
                    },
                ],
            }
        )
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmResponseError(f"unexpected response shape: {exc}") from exc
        if not isinstance(content, str):
            raise LlmResponseError("message content was not text")
        usage = payload.get("usage")
        try:
            # Some local servers omit usage; that costs 0 rather than failing.
            in_tok = int(usage.get("prompt_tokens", 0)) if isinstance(usage, dict) else 0
            out_tok = int(usage.get("completion_tokens", 0)) if isinstance(usage, dict) else 0
        except (TypeError, ValueError):
            in_tok = out_tok = 0
        return _decision_to_result(
            content,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=self._cost(in_tok, out_tok),
        )

    async def check_connectivity(self) -> None:
        """One-token chat completion: proves this endpoint serves this model."""
        payload = await self._post(
            {
                "model": self._model,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}],
            }
        )
        if not isinstance(payload.get("choices"), list):
            raise LlmResponseError("unexpected response shape: no choices")


def build_client(
    *,
    provider: str,
    api_key: str | None,
    base_url: str | None,
    model: str,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    prices: Prices | None = None,
) -> LlmClient:
    """Construct the adapter for a stored provider config."""
    if provider == "openai_compatible":
        return OpenAICompatibleClient(
            base_url=base_url or "",
            model=model,
            api_key=api_key,
            max_output_tokens=max_output_tokens,
            prices=prices,
        )
    return AnthropicClient(
        api_key=api_key or "", model=model, max_output_tokens=max_output_tokens, prices=prices
    )
