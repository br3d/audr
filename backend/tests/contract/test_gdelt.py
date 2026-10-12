"""Contract tests for the keyless GDELT 2.0 Doc API adapter (AUD-512)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import respx
from httpx import Response

from audr.providers.gdelt import GdeltArticle, GdeltProvider, GdeltRateLimited

_BASE = "https://api.gdeltproject.org/api/v2/doc/doc"


def _response(articles: list[dict]) -> dict:
    return {"articles": articles}


@pytest.mark.contract
async def test_search_news_parses_articles() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(_BASE).mock(
            return_value=Response(
                200,
                json=_response(
                    [
                        {
                            "url": "https://example.com/a1",
                            "title": "Ethereum upgrade ships",
                            "seendate": "20260102T030405Z",
                            "domain": "example.com",
                        }
                    ]
                ),
            )
        )
        async with GdeltProvider() as provider:
            items = await provider.search_news("Ethereum")

    assert items == [
        GdeltArticle(
            external_id="https://example.com/a1",
            title="Ethereum upgrade ships",
            url="https://example.com/a1",
            news_site="example.com",
            published_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        )
    ]


@pytest.mark.contract
async def test_search_news_sends_query_param() -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(_BASE).mock(return_value=Response(200, json=_response([])))
        async with GdeltProvider() as provider:
            await provider.search_news("UNI")

    sent = route.calls.last.request
    assert sent.url.params["query"] == "UNI"


@pytest.mark.contract
async def test_search_news_empty_results_returns_empty_list() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(_BASE).mock(return_value=Response(200, json=_response([])))
        async with GdeltProvider() as provider:
            items = await provider.search_news("NoSuchThing")
    assert items == []


@pytest.mark.contract
async def test_search_news_http_error_returns_empty_list_without_raising() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(_BASE).mock(return_value=Response(503))
        async with GdeltProvider() as provider:
            items = await provider.search_news("Ethereum")
    assert items == []


@pytest.mark.contract
async def test_search_news_network_error_returns_empty_list_without_raising() -> None:
    import httpx

    with respx.mock(assert_all_called=False) as mock:
        mock.get(_BASE).mock(side_effect=httpx.ConnectError("boom"))
        async with GdeltProvider() as provider:
            items = await provider.search_news("Ethereum")
    assert items == []


@pytest.mark.contract
async def test_search_news_raises_gdelt_rate_limited_on_429() -> None:
    """A 429 is distinct from every other failure this adapter swallows
    (AUD-515) — the caller needs to tell "rate limited" apart from "no
    results" in order to back off instead of burning through every asset.
    """
    with respx.mock(assert_all_called=False) as mock:
        mock.get(_BASE).mock(return_value=Response(429))
        async with GdeltProvider() as provider:
            with pytest.raises(GdeltRateLimited):
                await provider.search_news("Ethereum")


@pytest.mark.contract
async def test_search_news_malformed_json_returns_empty_list() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(_BASE).mock(return_value=Response(200, content=b"not json"))
        async with GdeltProvider() as provider:
            items = await provider.search_news("Ethereum")
    assert items == []


@pytest.mark.contract
async def test_search_news_skips_malformed_entries() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(_BASE).mock(
            return_value=Response(
                200,
                json=_response(
                    [
                        {
                            "url": "https://example.com/missing-title",
                            "seendate": "20260101T000000Z",
                        },
                        {
                            "url": "https://example.com/ok",
                            "title": "Fine",
                            "seendate": "20260101T000000Z",
                            "domain": "example.com",
                        },
                    ]
                ),
            )
        )
        async with GdeltProvider() as provider:
            items = await provider.search_news("Ethereum")
    assert [i.url for i in items] == ["https://example.com/ok"]


@pytest.mark.contract
async def test_search_news_sends_honest_user_agent() -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(_BASE).mock(return_value=Response(200, json=_response([])))
        async with GdeltProvider() as provider:
            await provider.search_news("Ethereum")
    sent = route.calls.last.request
    user_agent = sent.headers["User-Agent"]
    assert "audr" in user_agent
    assert "http" in user_agent
