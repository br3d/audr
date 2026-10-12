"""Atom/RSS normalisation into the existing asset_news shape (AUD-512).

Stdlib only (`xml.etree`) — feeds are a well-behaved, boring format and
callers already depend on httpx for the actual fetch. A feed that returns
non-feed content (a moved URL silently serving an HTML error page is the
common failure mode) raises FeedParseError rather than returning an empty
list, so the poller can tell "empty feed" from "broken feed" and charge the
per-feed health counter accordingly. An entry missing a required field is
skipped individually rather than failing the whole feed.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime


class FeedParseError(Exception):
    """Raised when the fetched body is not a recognisable Atom or RSS document."""


@dataclass(frozen=True)
class FeedItem:
    external_id: str
    title: str
    url: str
    published_at: datetime


def parse_feed(content: bytes) -> list[FeedItem]:
    """Parse an Atom or RSS 2.0 document into a list of FeedItem."""
    try:
        root = ET.fromstring(content)  # noqa: S314 — feed registry URLs, not arbitrary user XML
    except ET.ParseError as exc:
        raise FeedParseError(f"not well-formed XML: {exc}") from exc

    tag = _localname(root.tag)
    if tag == "feed":
        entries = _children(root, "entry")
        return [item for entry in entries if (item := _parse_atom_entry(entry)) is not None]
    if tag == "rss":
        channel = _child(root, "channel")
        if channel is None:
            raise FeedParseError("rss document has no <channel>")
        items = _children(channel, "item")
        return [item for elem in items if (item := _parse_rss_item(elem)) is not None]
    raise FeedParseError(f"unrecognised feed root element: <{tag}>")


def _parse_atom_entry(entry: ET.Element) -> FeedItem | None:
    title = _text(_child(entry, "title"))
    link = _alternate_link(entry)
    url = link.get("href") if link is not None else None
    external_id = _text(_child(entry, "id")) or url
    published_raw = _text(_child(entry, "published")) or _text(_child(entry, "updated"))
    published_at = _parse_iso8601(published_raw) if published_raw else None
    if not title or not url or not external_id or published_at is None:
        return None
    return FeedItem(external_id=external_id, title=title, url=url, published_at=published_at)


def _alternate_link(entry: ET.Element) -> ET.Element | None:
    links = _children(entry, "link")
    for link in links:
        rel = link.get("rel")
        if rel is None or rel == "alternate":
            return link
    return links[0] if links else None


def _parse_rss_item(item: ET.Element) -> FeedItem | None:
    title = _text(_child(item, "title"))
    url = _text(_child(item, "link"))
    external_id = _text(_child(item, "guid")) or url
    pub_raw = _text(_child(item, "pubDate"))
    published_at = _parse_rfc822(pub_raw) if pub_raw else None
    if not title or not url or not external_id or published_at is None:
        return None
    return FeedItem(external_id=external_id, title=title, url=url, published_at=published_at)


def _parse_iso8601(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _parse_rfc822(value: str) -> datetime | None:
    try:
        parsed = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(parent: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in parent if _localname(child.tag) == name]


def _child(parent: ET.Element, name: str) -> ET.Element | None:
    matches = _children(parent, name)
    return matches[0] if matches else None


def _text(elem: ET.Element | None) -> str | None:
    if elem is None or elem.text is None:
        return None
    text = elem.text.strip()
    return text or None
