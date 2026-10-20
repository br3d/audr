import httpx
import pytest
import respx

from audr_mcp.client import AudrClient, AudrClientError


@pytest.fixture
def client() -> AudrClient:
    return AudrClient("http://localhost:80", "audr_pat_x", timeout_seconds=1.0)


def test_has_no_mutating_methods(client: AudrClient) -> None:
    """Structural guarantee: nothing exists on this class to call but GET."""
    for verb in ("post", "put", "patch", "delete"):
        assert not hasattr(client, verb)


@respx.mock
async def test_get_sends_bearer_token_and_accept(client: AudrClient) -> None:
    route = respx.get("http://localhost:80/api/v1/llm/context").mock(
        return_value=httpx.Response(200, text="# Portfolio\n")
    )
    response = await client.get("/api/v1/llm/context", accept="text/markdown")
    assert response.text == "# Portfolio\n"
    sent = route.calls.last.request
    assert sent.headers["authorization"] == "Bearer audr_pat_x"
    assert sent.headers["accept"] == "text/markdown"


@respx.mock
async def test_get_forwards_params(client: AudrClient) -> None:
    respx.get("http://localhost:80/api/v1/history", params={"period": "30d"}).mock(
        return_value=httpx.Response(200, json={"period": "30d", "items": []})
    )
    response = await client.get("/api/v1/history", params={"period": "30d"})
    assert response.json()["period"] == "30d"


@respx.mock
async def test_get_raises_on_error_status(client: AudrClient) -> None:
    respx.get("http://localhost:80/api/v1/llm/asset/NOPE").mock(
        return_value=httpx.Response(404, text='{"detail":"asset not found"}')
    )
    with pytest.raises(AudrClientError) as exc_info:
        await client.get("/api/v1/llm/asset/NOPE")
    assert exc_info.value.status_code == 404
