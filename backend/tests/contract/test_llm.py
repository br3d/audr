"""Contract tests for the Anthropic Messages API adapter (AUD-658).

Every failure mode here must raise a distinct `LlmUnavailable` subclass —
`audr.jobs.news_gate` relies on catching exactly that base class to fail
open, so each failure mode is asserted individually rather than just
"raises something".
"""

from __future__ import annotations

import pytest
import respx
from httpx import Response

from audr.providers.llm import (
    AnthropicClient,
    LlmRateLimited,
    LlmResponseError,
    LlmTimeout,
    NoApiKey,
)

_URL = "https://api.anthropic.com/v1/messages"


def _message_response(decision: dict, *, input_tokens: int = 100, output_tokens: int = 20) -> dict:
    import json

    return {
        "content": [{"type": "text", "text": json.dumps(decision)}],
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


@pytest.mark.contract
async def test_classify_article_parses_decision() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.post(_URL).mock(
            return_value=Response(
                200,
                json=_message_response(
                    {
                        "important": True,
                        "importance": 80,
                        "summary": "Big hack.",
                        "reason": "exploit",
                    }
                ),
            )
        )
        client = AnthropicClient(api_key="sk-test")
        try:
            result = await client.classify_article(
                title="Protocol hacked", text="body", asset_symbol="ETH", asset_name="Ethereum"
            )
        finally:
            await client.close()

    assert result.important is True
    assert result.importance == 80
    assert result.summary == "Big hack."
    assert result.cost_usd > 0


@pytest.mark.contract
async def test_classify_article_clamps_importance_to_0_100() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.post(_URL).mock(
            return_value=Response(
                200, json=_message_response({"important": True, "importance": 500, "summary": "x"})
            )
        )
        client = AnthropicClient(api_key="sk-test")
        try:
            result = await client.classify_article(
                title="t", text="", asset_symbol="ETH", asset_name="Ethereum"
            )
        finally:
            await client.close()
    assert result.importance == 100


@pytest.mark.contract
async def test_classify_article_sends_api_key_header() -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(_URL).mock(
            return_value=Response(
                200, json=_message_response({"important": False, "importance": 10, "summary": ""})
            )
        )
        client = AnthropicClient(api_key="sk-secret-value")
        try:
            await client.classify_article(
                title="t", text="", asset_symbol="ETH", asset_name="Ethereum"
            )
        finally:
            await client.close()
    sent = route.calls.last.request
    assert sent.headers["x-api-key"] == "sk-secret-value"


@pytest.mark.contract
async def test_constructing_without_api_key_raises_no_api_key() -> None:
    with pytest.raises(NoApiKey):
        AnthropicClient(api_key="")


@pytest.mark.contract
async def test_classify_article_raises_no_api_key_on_401() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.post(_URL).mock(return_value=Response(401, json={"error": "bad key"}))
        client = AnthropicClient(api_key="sk-bad")
        try:
            with pytest.raises(NoApiKey):
                await client.classify_article(
                    title="t", text="", asset_symbol="ETH", asset_name="Ethereum"
                )
        finally:
            await client.close()


@pytest.mark.contract
async def test_classify_article_raises_rate_limited_after_exhausting_retries() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.post(_URL).mock(return_value=Response(429))
        client = AnthropicClient(api_key="sk-test", retries=1, retry_backoff_s=0)
        try:
            with pytest.raises(LlmRateLimited):
                await client.classify_article(
                    title="t", text="", asset_symbol="ETH", asset_name="Ethereum"
                )
        finally:
            await client.close()


@pytest.mark.contract
async def test_classify_article_raises_timeout_after_exhausting_retries() -> None:
    import httpx

    with respx.mock(assert_all_called=False) as mock:
        mock.post(_URL).mock(side_effect=httpx.ReadTimeout("boom"))
        client = AnthropicClient(api_key="sk-test", retries=1, retry_backoff_s=0)
        try:
            with pytest.raises(LlmTimeout):
                await client.classify_article(
                    title="t", text="", asset_symbol="ETH", asset_name="Ethereum"
                )
        finally:
            await client.close()


@pytest.mark.contract
async def test_classify_article_raises_response_error_on_non_json_content() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.post(_URL).mock(
            return_value=Response(
                200,
                json={
                    "content": [{"type": "text", "text": "not json"}],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            )
        )
        client = AnthropicClient(api_key="sk-test")
        try:
            with pytest.raises(LlmResponseError):
                await client.classify_article(
                    title="t", text="", asset_symbol="ETH", asset_name="Ethereum"
                )
        finally:
            await client.close()


@pytest.mark.contract
async def test_classify_article_raises_response_error_on_server_error() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.post(_URL).mock(return_value=Response(500))
        client = AnthropicClient(api_key="sk-test")
        try:
            with pytest.raises(LlmResponseError):
                await client.classify_article(
                    title="t", text="", asset_symbol="ETH", asset_name="Ethereum"
                )
        finally:
            await client.close()
