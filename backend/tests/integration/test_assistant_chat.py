"""POST /api/v1/assistant/chat with a fake LLM client (AUD-955)."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import text

from audr.api.app import app
from audr.api.assistant import MAX_CONTENT_CHARS, MAX_TURNS, get_assistant_client
from audr.providers.llm import ChatResult, LlmReasoningOnly, LlmTimeout, LlmUnavailable
from tests.integration.test_llm_api import (  # noqa: F401 -- fixtures
    _clean_tables,
    _mint_token_via_api,
    _seed_asset,
    _setup_and_login,
    http_client,
)

pytestmark = pytest.mark.integration

_URL = "/api/v1/assistant/chat"


class FakeClient:
    model = "fake-model"

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict] = []
        self.closed = False

    async def chat(self, *, system, messages, max_tokens):
        self.calls.append({"system": system, "messages": messages, "max_tokens": max_tokens})
        if self.error:
            raise self.error
        return ChatResult(text="Summary.\n- point", input_tokens=7, output_tokens=3, model="fake-model")

    async def close(self) -> None:
        self.closed = True


@pytest.fixture()
def fake():
    client = FakeClient()
    app.dependency_overrides[get_assistant_client] = lambda: client
    yield client
    app.dependency_overrides.pop(get_assistant_client, None)


async def test_409_llm_not_configured_without_integration(http_client) -> None:
    csrf = await _setup_and_login(http_client)
    r = await http_client.post(
        _URL, json={"mode": "portfolio", "messages": []}, headers={"x-csrf-token": csrf}
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "llm_not_configured"


async def test_requires_session(http_client, fake) -> None:
    r = await http_client.post(_URL, json={"mode": "portfolio", "messages": []})
    assert r.status_code == 401


async def test_requires_csrf(http_client, fake) -> None:
    await _setup_and_login(http_client)
    r = await http_client.post(_URL, json={"mode": "portfolio", "messages": []})
    assert r.status_code in (401, 403)
    assert fake.calls == []


async def test_api_token_cannot_use_it(http_client, fake) -> None:
    csrf = await _setup_and_login(http_client)
    token = await _mint_token_via_api(http_client, csrf)
    http_client.cookies.clear()
    r = await http_client.post(
        _URL,
        json={"mode": "portfolio", "messages": []},
        headers={"authorization": f"Bearer {token}"},
    )
    assert r.status_code in (401, 403)
    assert fake.calls == []


async def test_portfolio_mode_adds_default_request_and_context(http_client, fake) -> None:
    csrf = await _setup_and_login(http_client)
    r = await http_client.post(
        _URL, json={"mode": "portfolio", "messages": []}, headers={"x-csrf-token": csrf}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reply"] == "Summary.\n- point"
    assert body["mode"] == "portfolio" and body["model"] == "fake-model"
    assert body["usage"] == {"input_tokens": 7, "output_tokens": 3}
    assert body["context_as_of"] and body["elapsed_ms"] >= 0
    call = fake.calls[0]
    assert call["messages"] == [{"role": "user", "content": "Analyse my portfolio."}]
    assert "not financial advice" in call["system"] and "## Top allocations" in call["system"]
    assert "Recent headlines with analysis" not in call["system"]
    assert call["max_tokens"] == 1500
    assert fake.closed


async def test_news_mode_includes_wide_headlines_with_importance(
    http_client, db_session_factory, fake
) -> None:
    csrf = await _setup_and_login(http_client)
    asset_id = await _seed_asset(db_session_factory, "0x" + "b" * 40, "TKN")
    async with db_session_factory() as s, s.begin():
        await s.execute(
            text(
                "INSERT INTO asset_news (id, asset_id, source, external_id, title, url,"
                " news_site, published_at, summary, importance)"
                " VALUES (gen_random_uuid(), :a, 'gdelt', 'e1', 'TKN exploit', 'https://x.test/1',"
                " 'x.test', now(), 'Bridge drained.', 91)"
            ),
            {"a": asset_id},
        )
    r = await http_client.post(
        _URL, json={"mode": "news", "messages": []}, headers={"x-csrf-token": csrf}
    )
    assert r.status_code == 200, r.text
    call = fake.calls[0]
    assert call["messages"][0]["content"].startswith("What in the latest news")
    assert "Recent headlines with analysis (1)" in call["system"]
    assert "importance 91/100" in call["system"] and "Bridge drained." in call["system"]


async def test_chat_mode_needs_a_user_message_and_uses_history(http_client, fake) -> None:
    csrf = await _setup_and_login(http_client)
    h = {"x-csrf-token": csrf}
    r = await http_client.post(_URL, json={"mode": "chat", "messages": []}, headers=h)
    assert r.status_code == 422
    r = await http_client.post(
        _URL,
        json={"mode": "chat", "messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]},
        headers=h,
    )
    assert r.status_code == 422  # last turn must be the user's
    r = await http_client.post(
        _URL, json={"mode": "chat", "messages": [{"role": "user", "content": "hi"}]}, headers=h
    )
    assert r.status_code == 200
    assert fake.calls[0]["messages"] == [{"role": "user", "content": "hi"}]


async def test_history_is_trimmed_and_capped(http_client, fake) -> None:
    csrf = await _setup_and_login(http_client)
    msgs = []
    for i in range(14):
        msgs.append({"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"})
    msgs.append({"role": "user", "content": "x" * (MAX_CONTENT_CHARS + 500)})
    r = await http_client.post(
        _URL, json={"mode": "chat", "messages": msgs}, headers={"x-csrf-token": csrf}
    )
    assert r.status_code == 200
    sent = fake.calls[0]["messages"]
    assert len(sent) <= MAX_TURNS
    assert sent[0]["role"] == "user" and sent[-1]["role"] == "user"
    assert len(sent[-1]["content"]) == MAX_CONTENT_CHARS


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (LlmTimeout("t"), 504, "llm_timeout"),
        (LlmReasoningOnly("the model returned only reasoning"), 502, "llm_reasoning_only"),
        (LlmUnavailable("HTTP 500"), 502, "llm_error"),
    ],
)
async def test_provider_failures_map_to_502_504(http_client, error, status, code) -> None:
    client = FakeClient(error)
    app.dependency_overrides[get_assistant_client] = lambda: client
    try:
        csrf = await _setup_and_login(http_client)
        r = await http_client.post(
            _URL, json={"mode": "portfolio", "messages": []}, headers={"x-csrf-token": csrf}
        )
    finally:
        app.dependency_overrides.pop(get_assistant_client, None)
    assert r.status_code == status
    assert r.json()["detail"]["code"] == code
    assert client.closed
