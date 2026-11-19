# ruff: noqa: F811 -- fixtures imported from test_llm_api are re-declared as test parameters
"""POST /api/v1/assistant/chat with a fake LLM client (AUD-955)."""

from __future__ import annotations

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
        return ChatResult(
            text="Summary.\n- point", input_tokens=7, output_tokens=3, model="fake-model"
        )

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
    assert r.json()["error"]["code"] == "llm_not_configured"


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
    assert "financial advice" in call["system"]
    assert "Top allocations" in call["system"]
    assert "Recent headlines with analysis" not in call["system"]
    assert call["max_tokens"] == 4000
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
        json={
            "mode": "chat",
            "messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}],
        },
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
    assert r.json()["error"]["code"] == code
    assert client.closed


# --- AUD-958: server-side conversations -------------------------------------

_BASE_URL = "/api/v1/assistant"


async def _rows(db_session_factory, sql: str, **params):
    async with db_session_factory() as s:
        return [dict(r) for r in (await s.execute(text(sql), params)).mappings()]


@pytest.fixture(autouse=True)
async def _clean_assistant(db_session_factory):
    async def _wipe():
        async with db_session_factory() as s, s.begin():
            await s.execute(text("DELETE FROM assistant_conversation"))

    await _wipe()
    yield
    await _wipe()


async def _chat(client, csrf, **body):
    return await client.post(_URL, json=body, headers={"x-csrf-token": csrf})


async def test_new_conversation_is_created_and_stored(
    http_client, fake, db_session_factory
) -> None:
    csrf = await _setup_and_login(http_client)
    r = await _chat(
        http_client, csrf, mode="chat", messages=[{"role": "user", "content": "x" * 100}]
    )
    assert r.status_code == 200
    body = r.json()
    assert body["conversation_id"] and body["message_id"]
    convs = await _rows(db_session_factory, "SELECT * FROM assistant_conversation")
    assert len(convs) == 1 and convs[0]["mode"] == "chat" and convs[0]["title"] == "x" * 60
    msgs = await _rows(db_session_factory, "SELECT * FROM assistant_message ORDER BY seq")
    assert [(m["role"], m["status"]) for m in msgs] == [("user", "done"), ("assistant", "done")]
    assert msgs[1]["content"] == "Summary.\n- point" and msgs[1]["model"] == "fake-model"
    assert (msgs[1]["input_tokens"], msgs[1]["output_tokens"]) == (7, 3)
    assert str(msgs[1]["id"]) == body["message_id"]


async def test_quick_action_title(http_client, fake) -> None:
    csrf = await _setup_and_login(http_client)
    r = await _chat(http_client, csrf, mode="portfolio", messages=[])
    cid = r.json()["conversation_id"]
    detail = (await http_client.get(f"{_BASE_URL}/conversations/{cid}")).json()
    assert detail["title"] == "Analyse my portfolio"
    assert detail["messages"][0]["role"] == "user"


async def test_continuation_sends_stored_history(http_client, fake) -> None:
    csrf = await _setup_and_login(http_client)
    first = await _chat(
        http_client, csrf, mode="chat", messages=[{"role": "user", "content": "q1"}]
    )
    cid = first.json()["conversation_id"]
    r = await _chat(
        http_client,
        csrf,
        mode="chat",
        conversation_id=cid,
        messages=[{"role": "user", "content": "q2"}],
    )
    assert r.status_code == 200 and r.json()["conversation_id"] == cid
    assert fake.calls[-1]["messages"] == [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "Summary.\n- point"},
        {"role": "user", "content": "q2"},
    ]
    detail = (await http_client.get(f"{_BASE_URL}/conversations/{cid}")).json()
    assert len(detail["messages"]) == 4


async def test_continuation_unknown_conversation_404(http_client, fake) -> None:
    csrf = await _setup_and_login(http_client)
    r = await _chat(
        http_client,
        csrf,
        mode="chat",
        conversation_id="00000000-0000-0000-0000-000000000001",
        messages=[{"role": "user", "content": "q"}],
    )
    assert r.status_code == 404 and fake.calls == []


async def test_user_turn_is_stored_before_llm_call(http_client, db_session_factory) -> None:
    seen: dict = {}

    class Spy(FakeClient):
        async def chat(self, **kw):
            seen["rows"] = await _rows(
                db_session_factory, "SELECT role, status FROM assistant_message ORDER BY seq"
            )
            return await super().chat(**kw)

    app.dependency_overrides[get_assistant_client] = lambda: Spy()
    try:
        csrf = await _setup_and_login(http_client)
        r = await _chat(http_client, csrf, mode="chat", messages=[{"role": "user", "content": "q"}])
        assert r.status_code == 200
    finally:
        app.dependency_overrides.pop(get_assistant_client, None)
    assert seen["rows"] == [
        {"role": "user", "status": "done"},
        {"role": "assistant", "status": "pending"},
    ]


async def test_llm_failure_marks_row_error(http_client, db_session_factory) -> None:
    client = FakeClient(error=LlmTimeout("slow"))
    app.dependency_overrides[get_assistant_client] = lambda: client
    try:
        csrf = await _setup_and_login(http_client)
        r = await _chat(http_client, csrf, mode="chat", messages=[{"role": "user", "content": "q"}])
        assert r.status_code == 504
    finally:
        app.dependency_overrides.pop(get_assistant_client, None)
    msgs = await _rows(db_session_factory, "SELECT * FROM assistant_message ORDER BY seq")
    assert [m["status"] for m in msgs] == ["done", "error"]
    assert msgs[1]["error_code"] == "llm_timeout"
    # the failed turn is not replayed; the two user turns are merged so the
    # provider never sees two user messages in a row
    ok = FakeClient()
    app.dependency_overrides[get_assistant_client] = lambda: ok
    try:
        cid = str(msgs[0]["conversation_id"])
        r = await _chat(
            http_client,
            csrf,
            mode="chat",
            conversation_id=cid,
            messages=[{"role": "user", "content": "again"}],
        )
        assert r.status_code == 200
    finally:
        app.dependency_overrides.pop(get_assistant_client, None)
    assert ok.calls[0]["messages"] == [{"role": "user", "content": "q\n\nagain"}]


async def test_stale_pending_is_swept_on_read(http_client, fake, db_session_factory) -> None:
    csrf = await _setup_and_login(http_client)
    cid = (
        await _chat(http_client, csrf, mode="chat", messages=[{"role": "user", "content": "q"}])
    ).json()["conversation_id"]
    async with db_session_factory() as s, s.begin():
        await s.execute(
            text(
                "INSERT INTO assistant_message (conversation_id, role, status, created_at) "
                "VALUES (:c, 'assistant', 'pending', now() - interval '2 days')"
            ),
            {"c": cid},
        )
        await s.execute(
            text(
                "INSERT INTO assistant_message (conversation_id, role, status) "
                "VALUES (:c, 'assistant', 'pending')"
            ),
            {"c": cid},
        )
    detail = (await http_client.get(f"{_BASE_URL}/conversations/{cid}")).json()
    statuses = [(m["status"], m["error_code"]) for m in detail["messages"]][2:]
    assert statuses == [("error", "abandoned"), ("pending", None)]


async def test_list_rename_delete(http_client, fake) -> None:
    csrf = await _setup_and_login(http_client)
    h = {"x-csrf-token": csrf}
    ids = []
    for q in ("first", "second", "third"):
        r = await _chat(http_client, csrf, mode="chat", messages=[{"role": "user", "content": q}])
        ids.append(r.json()["conversation_id"])
    lst = (await http_client.get(f"{_BASE_URL}/conversations")).json()
    assert [c["title"] for c in lst["items"]] == ["third", "second", "first"]
    assert lst["items"][0]["message_count"] == 2 and lst["total"] == 3
    page = (await http_client.get(f"{_BASE_URL}/conversations?limit=1&offset=1")).json()
    assert [c["title"] for c in page["items"]] == ["second"] and page["total"] == 3

    r = await http_client.patch(
        f"{_BASE_URL}/conversations/{ids[0]}", json={"title": "Renamed"}, headers=h
    )
    assert r.status_code == 200 and r.json()["title"] == "Renamed"
    for bad in ("", "x" * 121):
        r = await http_client.patch(
            f"{_BASE_URL}/conversations/{ids[0]}", json={"title": bad}, headers=h
        )
        assert r.status_code == 422
    # renamed ordering: newest activity first, rename does not need to reorder tests
    r = await http_client.delete(f"{_BASE_URL}/conversations/{ids[1]}", headers=h)
    assert r.status_code == 204
    assert (await http_client.get(f"{_BASE_URL}/conversations/{ids[1]}")).status_code == 404
    assert (
        await http_client.delete(f"{_BASE_URL}/conversations/{ids[1]}", headers=h)
    ).status_code == 404
    r = await http_client.delete(f"{_BASE_URL}/conversations", headers=h)
    assert r.status_code == 204
    assert (await http_client.get(f"{_BASE_URL}/conversations")).json()["total"] == 0


async def test_mutations_need_csrf(http_client, fake) -> None:
    csrf = await _setup_and_login(http_client)
    cid = (
        await _chat(http_client, csrf, mode="chat", messages=[{"role": "user", "content": "q"}])
    ).json()["conversation_id"]
    for r in (
        await http_client.patch(f"{_BASE_URL}/conversations/{cid}", json={"title": "t"}),
        await http_client.delete(f"{_BASE_URL}/conversations/{cid}"),
        await http_client.delete(f"{_BASE_URL}/conversations"),
    ):
        assert r.status_code in (401, 403)
    assert (await http_client.get(f"{_BASE_URL}/conversations")).json()["total"] == 1


async def test_conversation_routes_reject_api_tokens_and_anonymous(http_client, fake) -> None:
    csrf = await _setup_and_login(http_client)
    cid = (
        await _chat(http_client, csrf, mode="chat", messages=[{"role": "user", "content": "q"}])
    ).json()["conversation_id"]
    token = await _mint_token_via_api(http_client, csrf)
    http_client.cookies.clear()
    for path in ("/conversations", f"/conversations/{cid}"):
        r = await http_client.get(_BASE_URL + path)
        assert r.status_code == 401
        r = await http_client.get(_BASE_URL + path, headers={"authorization": f"Bearer {token}"})
        assert r.status_code in (401, 403)


async def test_not_in_public_openapi(http_client) -> None:
    paths = (await http_client.get("/openapi.json")).json()["paths"]
    assert not [p for p in paths if "assistant" in p]


async def test_stateless_shape_still_works(http_client, fake) -> None:
    csrf = await _setup_and_login(http_client)
    r = await _chat(
        http_client,
        csrf,
        mode="chat",
        messages=[
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
        ],
    )
    assert r.status_code == 200
    assert [m["content"] for m in fake.calls[0]["messages"]] == ["a", "b", "c"]
    assert r.json()["reply"] == "Summary.\n- point"


# --- presets (AUD-961) -------------------------------------------------------

_PRESET_IDS = [
    "daily_brief",
    "concentration_risk",
    "news_impact",
    "alert_triage",
    "biggest_movers",
    "asset_deep_dive",
]


async def test_presets_registry_lists_exactly_six(http_client, fake) -> None:
    await _setup_and_login(http_client)
    r = await http_client.get("/api/v1/assistant/presets")
    assert r.status_code == 200
    items = r.json()
    assert [i["id"] for i in items] == _PRESET_IDS
    assert {i["id"] for i in items if i["needs_asset"]} == {"asset_deep_dive"}
    assert all(set(i) == {"id", "label", "description", "needs_asset"} for i in items)


async def test_presets_require_session(http_client, fake) -> None:
    assert (await http_client.get("/api/v1/assistant/presets")).status_code == 401


@pytest.mark.parametrize("preset", [p for p in _PRESET_IDS if p != "asset_deep_dive"])
async def test_chat_with_preset_uses_prompt_and_context(http_client, fake, preset) -> None:
    from audr.assistant.presets import PRESETS_BY_ID

    csrf = await _setup_and_login(http_client)
    r = await http_client.post(_URL, json={"preset": preset}, headers={"x-csrf-token": csrf})
    assert r.status_code == 200, r.text
    p = PRESETS_BY_ID[preset]
    assert r.json()["mode"] == p.mode
    call = fake.calls[0]
    assert call["messages"] == [{"role": "user", "content": p.user_prompt}]
    assert p.instructions in call["system"] and "Top allocations" in call["system"]
    wide = p.mode == "news" and not p.trim_news_context
    assert ("Recent headlines with analysis" in call["system"]) == wide
    assert "200 words" in call["system"] or preset != "daily_brief"
    expected = 8000 if preset == "alert_triage" else 4000
    assert call["max_tokens"] == expected


async def test_asset_deep_dive_puts_asset_context_in_call(
    http_client, db_session_factory, fake
) -> None:
    csrf = await _setup_and_login(http_client)
    await _seed_asset(db_session_factory, "0x" + "c" * 40, "DEEP")
    r = await http_client.post(
        _URL,
        json={"preset": "asset_deep_dive", "asset_ref": "deep"},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 200, r.text
    call = fake.calls[0]
    assert "DEEP" in call["system"] and "deep dive" in call["messages"][0]["content"]
    assert "what is unknown" in call["system"]


async def test_preset_error_paths(http_client, fake) -> None:
    csrf = await _setup_and_login(http_client)
    h = {"x-csrf-token": csrf}
    r = await http_client.post(_URL, json={"preset": "nope"}, headers=h)
    assert r.status_code == 422
    r = await http_client.post(_URL, json={"preset": "asset_deep_dive"}, headers=h)
    assert r.status_code == 422
    r = await http_client.post(_URL, json={"messages": []}, headers=h)
    assert r.status_code == 422
    r = await http_client.post(
        _URL, json={"preset": "asset_deep_dive", "asset_ref": "NOPE"}, headers=h
    )
    assert r.status_code == 404
    assert not fake.calls and fake.closed
    convs = await http_client.get("/api/v1/assistant/conversations")
    assert convs.json()["total"] == 0


async def test_preset_conversation_title_and_followup(http_client, fake) -> None:
    csrf = await _setup_and_login(http_client)
    h = {"x-csrf-token": csrf}
    r = await http_client.post(_URL, json={"preset": "alert_triage"}, headers=h)
    cid = r.json()["conversation_id"]
    detail = (await http_client.get(f"/api/v1/assistant/conversations/{cid}")).json()
    assert detail["title"] == "Alert triage"
    r = await http_client.post(
        _URL,
        json={
            "preset": "alert_triage",
            "conversation_id": cid,
            "messages": [{"role": "user", "content": "and the second one?"}],
        },
        headers=h,
    )
    assert r.status_code == 200
    assert fake.calls[1]["messages"][-1]["content"] == "and the second one?"


async def test_news_impact_context_is_trimmed(http_client, db_session_factory, fake) -> None:
    csrf = await _setup_and_login(http_client)
    asset_id = await _seed_asset(db_session_factory, "0x" + "d" * 40, "TRM")
    async with db_session_factory() as s, s.begin():
        for i in range(14):
            await s.execute(
                text(
                    "INSERT INTO asset_news (id, asset_id, source, external_id, title, url,"
                    " news_site, published_at, summary, importance)"
                    " VALUES (gen_random_uuid(), :a, 'gdelt', :e, :t, 'https://x.test/1',"
                    " 'x.test', now() - make_interval(mins => :i), 'SECRETSUMMARY', :imp)"
                ),
                {
                    "a": asset_id,
                    "e": f"e{i}",
                    "t": f"Headline {i}",
                    "i": i,
                    "imp": 90 if i < 12 else 10,
                },
            )
    r = await http_client.post(_URL, json={"preset": "news_impact"}, headers={"x-csrf-token": csrf})
    assert r.status_code == 200, r.text
    system = fake.calls[0]["system"]
    assert "## Alerts" not in system and "## Headlines\n" not in system
    full = system.split("## Headlines to assess")[1].split("## Ignored")[0]
    assert full.count("importance 90/100") == 10
    ignored = system.split("## Ignored headlines")[1].split("=== TASK RULES")[0]
    assert "Headline 10" in ignored and "Headline 13" in ignored
    assert "importance" not in ignored and "SECRETSUMMARY" not in system
    assert fake.calls[0]["max_tokens"] == 4000


# --- biggest_movers (AUD-963) -----------------------------------------------


async def test_biggest_movers_context_sorted_by_usd_with_unknown(
    http_client, db_session_factory, fake
) -> None:
    from decimal import Decimal

    from tests.integration.test_llm_api import _seed_snapshot, _seed_wallet

    csrf = await _setup_and_login(http_client)
    w = await _seed_wallet(db_session_factory)
    small = await _seed_asset(db_session_factory, "0x" + "a" * 40, "SMALL")  # +50% on 1 unit
    big = await _seed_asset(db_session_factory, "0x" + "b" * 40, "BIG")  # +10% on 10 units
    fresh = await _seed_asset(db_session_factory, "0x" + "d" * 40, "FRESH")  # no history
    one = "1000000000000000000"
    ten = "10000000000000000000"

    def h(asset, raw, price, value):
        return {
            "wallet_id": w,
            "asset_id": asset,
            "raw_amount": raw,
            "price_usd": price,
            "value_usd": value,
        }

    await _seed_snapshot(
        db_session_factory,
        holdings=[h(small, one, "2", "2"), h(big, ten, "110", "1100"), h(fresh, one, "5", "5")],
    )
    old = await _seed_snapshot(
        db_session_factory,
        snapshotted_at_sql="now() - interval '24 hours'",
        holdings=[h(small, one, "1", "1"), h(big, ten, "100", "1000")],
    )
    async with db_session_factory() as session, session.begin():
        # backdate publication too, or this would outrank the real latest snapshot
        await session.execute(
            text("UPDATE valuation_snapshot SET published_at = snapshotted_at WHERE id = :i"),
            {"i": old},
        )
    r = await http_client.post(
        _URL, json={"preset": "biggest_movers"}, headers={"x-csrf-token": csrf}
    )
    assert r.status_code == 200, r.text
    system = fake.calls[0]["system"]
    assert "Per-asset movers" in system and "300 words" in system
    assert "no news found" in system
    big_i, small_i, fresh_i = (
        system.index("- BIG: 24h"),
        system.index("- SMALL: 24h"),
        system.index("- FRESH: 24h"),
    )
    assert big_i < small_i < fresh_i  # by USD impact, not %; unknown last
    assert "- BIG: 24h +10.00% (+100.00 USD)" in system
    assert "- SMALL: 24h +100.00% (+1.00 USD)" in system
    assert "- FRESH: 24h unknown" in system
    assert "No 24h price history for: FRESH" in system
    assert "7d unknown" in system and "No snapshot close to 7d ago" in system
    assert Decimal("1")  # keep import used
