import inspect
import json

import pytest

import audr_mcp.server as server_module
from audr_mcp.config import Config
from audr_mcp.server import build_server

_EXPECTED_TOOL_NAMES = {
    "get_portfolio",
    "list_holdings",
    "get_asset",
    "get_history",
    "list_news",
    "list_alerts",
    "get_status",
}

_CONFIG = Config(
    base_url="http://localhost:80",
    token="audr_pat_x",
    timeout_seconds=10.0,
    mcp_enabled=False,
    http_host="127.0.0.1",
    http_port=8800,
)


class _Response:
    def __init__(self, text: str) -> None:
        self.text = text

    def json(self) -> dict:
        return json.loads(self.text)


class _FakeAudrClient:
    """Records every GET it is asked to make; never issues a real request."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict | None, str]] = []

    async def get(self, path: str, *, params: dict | None = None, accept: str = "application/json"):
        self.calls.append((path, params, accept))
        if path.startswith("/api/v1/llm/asset/") and accept == "application/json":
            return _Response(json.dumps({"asset_id": "11111111-1111-1111-1111-111111111111"}))
        return _Response(f"ok: {path}")


@pytest.fixture
def fake_client() -> _FakeAudrClient:
    return _FakeAudrClient()


def test_no_mutating_http_verb_appears_in_server_source() -> None:
    """Defense in depth on top of AudrClient's structural guarantee: no tool
    implementation here may even spell a mutating verb."""
    source = inspect.getsource(server_module)
    for verb in (".post(", ".put(", ".patch(", ".delete("):
        assert verb not in source


async def test_lists_exactly_the_documented_tools(fake_client: _FakeAudrClient) -> None:
    server = build_server(_CONFIG, client=fake_client)
    tools = await server.list_tools()
    assert {t.name for t in tools} == _EXPECTED_TOOL_NAMES


async def test_lists_the_portfolio_resource(fake_client: _FakeAudrClient) -> None:
    server = build_server(_CONFIG, client=fake_client)
    resources = await server.list_resources()
    assert [str(r.uri) for r in resources] == ["audr://portfolio/current"]


async def test_get_portfolio_reads_llm_context_as_markdown(fake_client: _FakeAudrClient) -> None:
    server = build_server(_CONFIG, client=fake_client)
    result = await server.call_tool("get_portfolio", {})
    assert result.content[0].text == "ok: /api/v1/llm/context"
    assert fake_client.calls == [("/api/v1/llm/context", None, "text/markdown")]


async def test_get_asset_passes_ref_through(fake_client: _FakeAudrClient) -> None:
    server = build_server(_CONFIG, client=fake_client)
    result = await server.call_tool("get_asset", {"ref": "ETH"})
    assert result.content[0].text == "ok: /api/v1/llm/asset/ETH"
    assert fake_client.calls == [("/api/v1/llm/asset/ETH", None, "text/markdown")]


async def test_get_history_defaults_period_to_7d(fake_client: _FakeAudrClient) -> None:
    server = build_server(_CONFIG, client=fake_client)
    await server.call_tool("get_history", {})
    assert fake_client.calls == [("/api/v1/history", {"period": "7d"}, "application/json")]


async def test_list_news_resolves_asset_ref_before_reading_news(
    fake_client: _FakeAudrClient,
) -> None:
    server = build_server(_CONFIG, client=fake_client)
    await server.call_tool("list_news", {"asset": "ETH", "limit": 5})
    assert fake_client.calls == [
        ("/api/v1/llm/asset/ETH", None, "application/json"),
        (
            "/api/v1/assets/11111111-1111-1111-1111-111111111111/news",
            {"limit": 5},
            "application/json",
        ),
    ]


async def test_list_alerts_forwards_filters(fake_client: _FakeAudrClient) -> None:
    server = build_server(_CONFIG, client=fake_client)
    await server.call_tool("list_alerts", {"unread_only": True, "limit": 3})
    assert fake_client.calls == [
        ("/api/v1/alerts", {"unread": True, "limit": 3}, "application/json")
    ]


async def test_get_status_reads_status_route(fake_client: _FakeAudrClient) -> None:
    server = build_server(_CONFIG, client=fake_client)
    await server.call_tool("get_status", {})
    assert fake_client.calls == [("/api/v1/status", None, "application/json")]


async def test_portfolio_resource_reads_llm_context_as_markdown(
    fake_client: _FakeAudrClient,
) -> None:
    server = build_server(_CONFIG, client=fake_client)
    contents = await server.read_resource("audr://portfolio/current")
    assert contents[0].content == "ok: /api/v1/llm/context"
    assert fake_client.calls == [("/api/v1/llm/context", None, "text/markdown")]
