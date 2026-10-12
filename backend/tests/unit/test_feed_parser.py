"""Unit tests for Atom/RSS normalisation (AUD-512)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from audr.providers.feed_parser import FeedItem, FeedParseError, parse_feed

_ATOM_SAMPLE = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>example/repo releases</title>
  <entry>
    <id>tag:github.com,2008:Repository/1/v1.2.3</id>
    <title>v1.2.3</title>
    <link href="https://github.com/example/repo/releases/tag/v1.2.3" rel="alternate"/>
    <updated>2026-01-02T03:04:05Z</updated>
    <content>Release notes</content>
  </entry>
  <entry>
    <id>tag:github.com,2008:Repository/1/v1.2.2</id>
    <title>v1.2.2</title>
    <link href="https://github.com/example/repo/releases/tag/v1.2.2" rel="alternate"/>
    <published>2026-01-01T00:00:00Z</published>
  </entry>
</feed>
"""

_RSS_SAMPLE = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <title>Example Press</title>
    <item>
      <title>Big news</title>
      <link>https://example.com/articles/1</link>
      <guid isPermaLink="false">example-1</guid>
      <pubDate>Thu, 01 Jan 2026 00:00:00 GMT</pubDate>
      <description>Something happened</description>
    </item>
  </channel>
</rss>
"""

_HTML_ERROR_PAGE = b"""<html><head><title>404</title></head>
<body><h1>Not Found</h1></body></html>
"""


@pytest.mark.unit
class TestParseFeed:
    def test_parses_atom_entries(self) -> None:
        items = parse_feed(_ATOM_SAMPLE)
        assert items == [
            FeedItem(
                external_id="tag:github.com,2008:Repository/1/v1.2.3",
                title="v1.2.3",
                url="https://github.com/example/repo/releases/tag/v1.2.3",
                published_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
            ),
            FeedItem(
                external_id="tag:github.com,2008:Repository/1/v1.2.2",
                title="v1.2.2",
                url="https://github.com/example/repo/releases/tag/v1.2.2",
                published_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
            ),
        ]

    def test_parses_rss_items(self) -> None:
        items = parse_feed(_RSS_SAMPLE)
        assert items == [
            FeedItem(
                external_id="example-1",
                title="Big news",
                url="https://example.com/articles/1",
                published_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
            )
        ]

    def test_rss_falls_back_to_link_when_guid_missing(self) -> None:
        rss = b"""<rss version="2.0"><channel><item>
          <title>No guid</title>
          <link>https://example.com/no-guid</link>
          <pubDate>Thu, 01 Jan 2026 00:00:00 GMT</pubDate>
        </item></channel></rss>"""
        items = parse_feed(rss)
        assert items[0].external_id == "https://example.com/no-guid"

    def test_malformed_html_error_page_raises(self) -> None:
        with pytest.raises(FeedParseError):
            parse_feed(_HTML_ERROR_PAGE)

    def test_non_xml_garbage_raises(self) -> None:
        with pytest.raises(FeedParseError):
            parse_feed(b"not xml at all {{{")

    def test_empty_feed_returns_empty_list(self) -> None:
        empty = b'<feed xmlns="http://www.w3.org/2005/Atom"><title>Empty</title></feed>'
        assert parse_feed(empty) == []

    def test_skips_entries_missing_title(self) -> None:
        atom = b"""<feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <id>no-title</id>
            <link href="https://example.com/x" rel="alternate"/>
            <updated>2026-01-01T00:00:00Z</updated>
          </entry>
        </feed>"""
        assert parse_feed(atom) == []

    def test_skips_entries_missing_date(self) -> None:
        atom = b"""<feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <id>no-date</id>
            <title>No date</title>
            <link href="https://example.com/x" rel="alternate"/>
          </entry>
        </feed>"""
        assert parse_feed(atom) == []

    def test_atom_without_namespace_still_parses(self) -> None:
        atom = b"""<feed>
          <entry>
            <id>bare-1</id>
            <title>Bare feed</title>
            <link href="https://example.com/bare" rel="alternate"/>
            <updated>2026-01-01T00:00:00Z</updated>
          </entry>
        </feed>"""
        items = parse_feed(atom)
        assert items[0].external_id == "bare-1"
