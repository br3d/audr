"""Unit tests for the shared provider HTTP plumbing (AUD-691)."""

from __future__ import annotations

import httpx
import pytest

from audr.providers.http_base import NEWS_USER_AGENT, ProviderHttpBase, RateLimitError

pytestmark = pytest.mark.unit


async def test_owned_client_gets_timeout_and_user_agent() -> None:
    base = ProviderHttpBase(timeout=7.0, user_agent=NEWS_USER_AGENT)
    assert base.client.headers["user-agent"] == NEWS_USER_AGENT
    assert base.client.timeout == httpx.Timeout(7.0)
    await base.close()
    assert base.client.is_closed


async def test_no_user_agent_sends_no_header() -> None:
    base = ProviderHttpBase()
    default_user_agent = base.client.headers.get("user-agent", "")
    assert default_user_agent == "" or default_user_agent.startswith("python-httpx")
    await base.close()


async def test_borrowed_client_is_not_closed() -> None:
    borrowed = httpx.AsyncClient()
    base = ProviderHttpBase(http_client=borrowed)
    assert base.client is borrowed
    await base.close()
    assert not borrowed.is_closed
    await borrowed.aclose()


async def test_context_manager_closes_owned_client() -> None:
    async with ProviderHttpBase() as base:
        client = base.client
    assert client.is_closed


def test_rate_limit_error_is_an_exception() -> None:
    with pytest.raises(RateLimitError):
        raise RateLimitError("429")
