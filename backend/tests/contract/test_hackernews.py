"""Contract tests for the keyless Hacker News (Algolia) adapter (AUD-656)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx
from httpx import Response

from audr.providers.hackernews import (
    HackerNewsProvider,
    HackerNewsRateLimited,
    HackerNewsStory,
)

_BASE = "https://hn.algolia.com/api/v1/search_by_date"


def _response(hits: list[dict]) -> dict:
    return {"hits": hits}


@pytest.mark.contract
async def test_search_stories_parses_hits() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(_BASE).mock(
            return_value=Response(
                200,
                json=_response(
                    [
                        {
                            "objectID": "12345",
                            "title": "Ethereum upgrade ships",
                            "url": "https://example.com/a1",
                            "points": 42,
                            "num_comments": 7,
                            "created_at_i": 1767321845,
                        }
                    ]
                ),
            )
        )
        async with HackerNewsProvider() as provider:
            items = await provider.search_stories('"Ethereum"')

    assert items == [
        HackerNewsStory(
            external_id="12345",
            title="Ethereum upgrade ships",
            url="https://example.com/a1",
            points=42,
            num_comments=7,
            published_at=datetime.fromtimestamp(1767321845, tz=UTC),
        )
    ]


@pytest.mark.contract
async def test_search_stories_falls_back_to_item_page_when_no_url() -> None:
    """Ask HN / Show HN text posts have no `url` field — the item's own
    discussion page is the only link that exists for them.
    """
    with respx.mock(assert_all_called=False) as mock:
        mock.get(_BASE).mock(
            return_value=Response(
                200,
                json=_response(
                    [
                        {
                            "objectID": "999",
                            "title": "Ask HN: anything",
                            "points": 10,
                            "num_comments": 3,
                            "created_at_i": 1767321845,
                        }
                    ]
                ),
            )
        )
        async with HackerNewsProvider() as provider:
            items = await provider.search_stories('"Ethereum"')

    assert items[0].url == "https://news.ycombinator.com/item?id=999"


@pytest.mark.contract
async def test_search_stories_sends_query_tags_and_title_restriction() -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(_BASE).mock(return_value=Response(200, json=_response([])))
        async with HackerNewsProvider() as provider:
            await provider.search_stories('"Uniswap"')

    sent = route.calls.last.request
    assert sent.url.params["query"] == '"Uniswap"'
    assert sent.url.params["tags"] == "story"
    assert sent.url.params["restrictSearchableAttributes"] == "title"


@pytest.mark.contract
async def test_search_stories_empty_results_returns_empty_list() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(_BASE).mock(return_value=Response(200, json=_response([])))
        async with HackerNewsProvider() as provider:
            items = await provider.search_stories('"NoSuchThing"')
    assert items == []


@pytest.mark.contract
async def test_search_stories_http_error_returns_empty_list_without_raising() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(_BASE).mock(return_value=Response(503))
        async with HackerNewsProvider() as provider:
            items = await provider.search_stories('"Ethereum"')
    assert items == []


@pytest.mark.contract
async def test_search_stories_network_error_returns_empty_list_without_raising() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(_BASE).mock(side_effect=httpx.ConnectError("boom"))
        async with HackerNewsProvider() as provider:
            items = await provider.search_stories('"Ethereum"')
    assert items == []


@pytest.mark.contract
async def test_search_stories_raises_rate_limited_on_429() -> None:
    """A 429 must be distinguishable from "no results" so the caller can back
    off instead of burning through every held asset (same discipline as
    `GdeltRateLimited`, AUD-515).
    """
    with respx.mock(assert_all_called=False) as mock:
        mock.get(_BASE).mock(return_value=Response(429))
        async with HackerNewsProvider() as provider:
            with pytest.raises(HackerNewsRateLimited):
                await provider.search_stories('"Ethereum"')


@pytest.mark.contract
async def test_search_stories_malformed_json_returns_empty_list() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(_BASE).mock(return_value=Response(200, content=b"not json"))
        async with HackerNewsProvider() as provider:
            items = await provider.search_stories('"Ethereum"')
    assert items == []


@pytest.mark.contract
async def test_search_stories_skips_entries_missing_title_or_object_id() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(_BASE).mock(
            return_value=Response(
                200,
                json=_response(
                    [
                        {
                            "objectID": "1",
                            "url": "https://example.com/missing-title",
                            "points": 1,
                            "num_comments": 0,
                            "created_at_i": 1767321845,
                        },
                        {
                            "title": "Missing objectID",
                            "url": "https://example.com/missing-id",
                            "points": 1,
                            "num_comments": 0,
                            "created_at_i": 1767321845,
                        },
                        {
                            "objectID": "2",
                            "title": "Fine",
                            "url": "https://example.com/ok",
                            "points": 1,
                            "num_comments": 0,
                            "created_at_i": 1767321845,
                        },
                    ]
                ),
            )
        )
        async with HackerNewsProvider() as provider:
            items = await provider.search_stories('"Ethereum"')
    assert [i.external_id for i in items] == ["2"]


@pytest.mark.contract
async def test_search_stories_defaults_missing_points_and_comments_to_zero() -> None:
    """Job/poll-shaped entries (excluded by `tags=story` in practice, but the
    adapter must not crash if one slips through) carry no `points` field.
    """
    with respx.mock(assert_all_called=False) as mock:
        mock.get(_BASE).mock(
            return_value=Response(
                200,
                json=_response(
                    [
                        {
                            "objectID": "1",
                            "title": "No engagement fields",
                            "url": "https://example.com/1",
                            "created_at_i": 1767321845,
                        }
                    ]
                ),
            )
        )
        async with HackerNewsProvider() as provider:
            items = await provider.search_stories('"Ethereum"')
    assert items[0].points == 0
    assert items[0].num_comments == 0


@pytest.mark.contract
async def test_search_stories_sends_honest_user_agent() -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(_BASE).mock(return_value=Response(200, json=_response([])))
        async with HackerNewsProvider() as provider:
            await provider.search_stories('"Ethereum"')
    sent = route.calls.last.request
    user_agent = sent.headers["User-Agent"]
    assert "audr" in user_agent
    assert "http" in user_agent
