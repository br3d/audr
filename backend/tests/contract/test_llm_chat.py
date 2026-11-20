"""Contract tests for the free-form `chat()` on both wire formats (AUD-955)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from httpx import Response

from audr.providers.llm import (
    AnthropicClient,
    LlmReasoningOnly,
    LlmResponseError,
    LlmTimeout,
    OpenAICompatibleClient,
)

pytestmark = pytest.mark.contract

_OAI = "https://llm.example.test/v1/chat/completions"
_ANT = "https://api.anthropic.com/v1/messages"
_TURNS = [{"role": "user", "content": "hi"}]


def _oai(**message: object) -> dict:
    return {"choices": [{"message": {"role": "assistant", **message}}]}


async def _oai_chat(body: dict):
    client = OpenAICompatibleClient(base_url="https://llm.example.test/v1", model="m", api_key="k")
    with respx.mock() as mock:
        route = mock.post(_OAI).mock(return_value=Response(200, json=body))
        try:
            return await client.chat(system="SYS", messages=_TURNS, max_tokens=77), route
        finally:
            await client.close()


async def test_openai_sends_system_first_and_returns_usage() -> None:
    result, route = await _oai_chat(
        {**_oai(content="hello"), "usage": {"prompt_tokens": 5, "completion_tokens": 2}}
    )
    sent = json.loads(route.calls[0].request.content)
    assert sent["max_tokens"] == 77
    assert sent["messages"][0] == {"role": "system", "content": "SYS"}
    assert sent["messages"][1:] == _TURNS
    assert (result.text, result.input_tokens, result.output_tokens, result.model) == (
        "hello",
        5,
        2,
        "m",
    )


async def test_openai_strips_think_blocks() -> None:
    result, _ = await _oai_chat(_oai(content="<think>plan\nplan</think>\n\nAnswer."))
    assert result.text == "Answer."


async def test_openai_missing_usage_is_zero() -> None:
    result, _ = await _oai_chat(_oai(content="ok"))
    assert (result.input_tokens, result.output_tokens) == (0, 0)


@pytest.mark.parametrize("key", ["reasoning", "reasoning_content"])
async def test_openai_reasoning_only_is_explicit_error(key: str) -> None:
    with pytest.raises(LlmReasoningOnly, match="only reasoning"):
        await _oai_chat(_oai(content="", **{key: "thinking..."}))


async def test_openai_unterminated_think_is_reasoning_only() -> None:
    with pytest.raises(LlmReasoningOnly):
        await _oai_chat(_oai(content="<think>still going"))


async def test_openai_empty_without_reasoning_is_error() -> None:
    with pytest.raises(LlmResponseError):
        await _oai_chat(_oai(content=""))


async def test_openai_timeout_is_llm_timeout() -> None:
    client = OpenAICompatibleClient(
        base_url="https://llm.example.test/v1", model="m", retries=0, timeout=1
    )
    with respx.mock() as mock:
        mock.post(_OAI).mock(side_effect=httpx.ReadTimeout("slow"))
        with pytest.raises(LlmTimeout):
            await client.chat(system="s", messages=_TURNS, max_tokens=5)
    await client.close()


async def _ant_chat(body: dict):
    client = AnthropicClient(api_key="sk-ant-x", model="claude-haiku-4-5")
    with respx.mock() as mock:
        route = mock.post(_ANT).mock(return_value=Response(200, json=body))
        try:
            return await client.chat(system="SYS", messages=_TURNS, max_tokens=99), route
        finally:
            await client.close()


async def test_anthropic_chat_joins_text_blocks_and_sends_system() -> None:
    result, route = await _ant_chat(
        {
            "content": [
                {"type": "thinking", "thinking": "hmm"},
                {"type": "text", "text": "<think>x</think>Part one. "},
                {"type": "text", "text": "Part two."},
            ],
            "usage": {"input_tokens": 11, "output_tokens": 4},
        }
    )
    sent = json.loads(route.calls[0].request.content)
    assert sent["system"] == "SYS" and sent["max_tokens"] == 99 and sent["messages"] == _TURNS
    assert result.text == "Part one. Part two."
    assert (result.input_tokens, result.output_tokens) == (11, 4)


async def test_anthropic_chat_missing_usage_is_zero() -> None:
    result, _ = await _ant_chat({"content": [{"type": "text", "text": "ok"}]})
    assert (result.input_tokens, result.output_tokens) == (0, 0)


async def test_anthropic_chat_empty_is_error() -> None:
    with pytest.raises(LlmResponseError):
        await _ant_chat({"content": []})


async def test_openai_chat_switches_thinking_off() -> None:
    """AUD-966: DeepSeek/Qwen on vLLM think by default and burn the budget."""
    _, route = await _oai_chat(_oai(content="ok"))
    sent = json.loads(route.calls[0].request.content)
    assert sent["chat_template_kwargs"] == {"thinking": False, "enable_thinking": False}


@pytest.mark.parametrize("status", [400, 422])
async def test_openai_chat_retries_without_template_kwargs_when_rejected(status: int) -> None:
    client = OpenAICompatibleClient(base_url="https://llm.example.test/v1", model="m", api_key="k")
    with respx.mock() as mock:
        route = mock.post(_OAI).mock(
            side_effect=[
                Response(status, json={"error": "unknown field"}),
                Response(200, json=_oai(content="ok")),
            ]
        )
        try:
            result = await client.chat(system="SYS", messages=_TURNS, max_tokens=5)
            assert result.text == "ok"
            assert "chat_template_kwargs" in json.loads(route.calls[0].request.content)
            assert "chat_template_kwargs" not in json.loads(route.calls[1].request.content)
            # Remembered: the next call goes straight to the plain body.
            mock.post(_OAI).mock(return_value=Response(200, json=_oai(content="again")))
            await client.chat(system="SYS", messages=_TURNS, max_tokens=5)
            assert "chat_template_kwargs" not in json.loads(mock.calls[-1].request.content)
        finally:
            await client.close()


async def test_openai_chat_other_errors_are_not_retried() -> None:
    client = OpenAICompatibleClient(
        base_url="https://llm.example.test/v1", model="m", api_key="k", retries=0
    )
    with respx.mock() as mock:
        route = mock.post(_OAI).mock(return_value=Response(500))
        try:
            with pytest.raises(LlmResponseError):
                await client.chat(system="SYS", messages=_TURNS, max_tokens=5)
            assert route.call_count == 1
        finally:
            await client.close()
