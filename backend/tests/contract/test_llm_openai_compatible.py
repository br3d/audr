"""Contract tests for the OpenAI-compatible adapter (AUD-932).

Same fail-open contract as the Anthropic adapter: every failure is a distinct
`LlmUnavailable` subclass.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
import respx
from httpx import Response

from audr.providers.llm import (
    LlmRateLimited,
    LlmResponseError,
    LlmUnavailable,
    NoApiKey,
    OpenAICompatibleClient,
)

_BASE = "https://llm.example.test/v1"
_URL = f"{_BASE}/chat/completions"
_DECISION = {"important": True, "importance": 80, "summary": "Big hack.", "reason": "exploit"}


def _completion(content: str, usage: dict | None = None) -> dict:
    body: dict = {"choices": [{"message": {"role": "assistant", "content": content}}]}
    if usage is not None:
        body["usage"] = usage
    return body


async def _classify(client: OpenAICompatibleClient):
    try:
        return await client.classify_article(
            title="t", text="body", asset_symbol="ETH", asset_name="Ethereum"
        )
    finally:
        await client.close()


@pytest.mark.contract
async def test_success_parses_decision_and_sends_bearer_and_json_mode() -> None:
    with respx.mock() as mock:
        route = mock.post(_URL).mock(
            return_value=Response(
                200,
                json=_completion(
                    json.dumps(_DECISION), {"prompt_tokens": 100, "completion_tokens": 20}
                ),
            )
        )
        result = await _classify(
            OpenAICompatibleClient(base_url=_BASE + "/", model="m", api_key="sk-x")
        )
    assert result.important is True and result.importance == 80
    assert result.summary == "Big hack."
    assert result.cost_usd == Decimal(0)  # unknown model => no Claude price
    sent = route.calls.last.request
    assert sent.headers["authorization"] == "Bearer sk-x"
    body = json.loads(sent.content)
    assert body["model"] == "m"
    assert body["response_format"] == {"type": "json_object"}


@pytest.mark.contract
async def test_no_key_sends_no_authorization_header() -> None:
    with respx.mock() as mock:
        route = mock.post(_URL).mock(
            return_value=Response(200, json=_completion(json.dumps(_DECISION)))
        )
        await _classify(OpenAICompatibleClient(base_url=_BASE, model="m"))
    assert "authorization" not in route.calls.last.request.headers


@pytest.mark.contract
async def test_stored_prices_drive_cost() -> None:
    with respx.mock() as mock:
        mock.post(_URL).mock(
            return_value=Response(
                200,
                json=_completion(
                    json.dumps(_DECISION),
                    {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
                ),
            )
        )
        result = await _classify(
            OpenAICompatibleClient(
                base_url=_BASE, model="m", prices=(Decimal("0.5"), Decimal("1.5"))
            )
        )
    assert result.cost_usd == Decimal(2)


@pytest.mark.contract
async def test_401_raises_no_api_key() -> None:
    with respx.mock() as mock:
        mock.post(_URL).mock(return_value=Response(401))
        with pytest.raises(NoApiKey):
            await _classify(OpenAICompatibleClient(base_url=_BASE, model="m", api_key="bad"))


@pytest.mark.contract
async def test_429_retries_then_succeeds() -> None:
    with respx.mock() as mock:
        route = mock.post(_URL).mock(
            side_effect=[Response(429), Response(200, json=_completion(json.dumps(_DECISION)))]
        )
        result = await _classify(
            OpenAICompatibleClient(base_url=_BASE, model="m", retry_backoff_s=0)
        )
    assert route.call_count == 2
    assert result.importance == 80


@pytest.mark.contract
async def test_429_on_every_retry_raises_rate_limited() -> None:
    with respx.mock() as mock:
        mock.post(_URL).mock(return_value=Response(429))
        with pytest.raises(LlmRateLimited):
            await _classify(
                OpenAICompatibleClient(base_url=_BASE, model="m", retries=1, retry_backoff_s=0)
            )


@pytest.mark.contract
async def test_non_json_content_raises_response_error() -> None:
    with respx.mock() as mock:
        mock.post(_URL).mock(
            return_value=Response(200, json=_completion("I cannot help with that"))
        )
        with pytest.raises(LlmResponseError):
            await _classify(OpenAICompatibleClient(base_url=_BASE, model="m"))


@pytest.mark.contract
@pytest.mark.parametrize(
    "content",
    [
        "```json\n" + json.dumps(_DECISION) + "\n```",
        "Sure! Here is the result:\n" + json.dumps(_DECISION) + "\nHope that helps.",
    ],
)
async def test_fenced_or_prose_wrapped_json_is_tolerated(content: str) -> None:
    with respx.mock() as mock:
        mock.post(_URL).mock(return_value=Response(200, json=_completion(content)))
        result = await _classify(OpenAICompatibleClient(base_url=_BASE, model="m"))
    assert result.importance == 80


@pytest.mark.contract
async def test_missing_usage_costs_zero_instead_of_failing() -> None:
    with respx.mock() as mock:
        mock.post(_URL).mock(return_value=Response(200, json=_completion(json.dumps(_DECISION))))
        result = await _classify(
            OpenAICompatibleClient(base_url=_BASE, model="m", prices=(Decimal(1), Decimal(1)))
        )
    assert result.cost_usd == Decimal(0)


@pytest.mark.contract
async def test_unexpected_shape_raises_response_error() -> None:
    with respx.mock() as mock:
        mock.post(_URL).mock(return_value=Response(200, json={"choices": []}))
        with pytest.raises(LlmResponseError):
            await _classify(OpenAICompatibleClient(base_url=_BASE, model="m"))


@pytest.mark.contract
async def test_check_connectivity_ok_and_error() -> None:
    with respx.mock() as mock:
        mock.post(_URL).mock(return_value=Response(200, json=_completion("pong")))
        client = OpenAICompatibleClient(base_url=_BASE, model="m")
        await client.check_connectivity()
        mock.post(_URL).mock(return_value=Response(404))
        with pytest.raises(LlmUnavailable):
            await client.check_connectivity()
        await client.close()
