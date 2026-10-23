"""Snapshot governance GraphQL adapter — keyless (AUD-513).

`hub.snapshot.org/graphql` publishes no rate limit or SLA, same as GDELT
(docs/news-sources.md §3/§6): a request failure, non-2xx response, GraphQL
error, or malformed body is raised as `SnapshotError` so the caller can
record a per-space health counter, rather than silently returning an empty
list — unlike GDELT there is a per-space registry row here to mark stale.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from audr.providers.http_base import NEWS_USER_AGENT, ProviderHttpBase

logger = logging.getLogger(__name__)

_BASE_URL = "https://hub.snapshot.org/graphql"
_TIMEOUT = 15.0
_USER_AGENT = NEWS_USER_AGENT
_MAX_PROPOSALS = 20

_QUERY = """
query Proposals($space: String!, $first: Int!) {
  proposals(
    first: $first
    where: { space: $space }
    orderBy: "created"
    orderDirection: desc
  ) {
    id
    title
    body
    state
    start
    end
  }
}
"""


class SnapshotError(Exception):
    """Raised on any request/response failure — callers record it as a health counter."""


@dataclass(frozen=True)
class SnapshotProposal:
    id: str
    title: str
    summary: str
    state: str
    start: datetime
    end: datetime
    link: str


class SnapshotProvider:
    """Read-only, keyless Snapshot Hub GraphQL client."""

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        base_url: str = _BASE_URL,
        user_agent: str = _USER_AGENT,
    ) -> None:
        self._base = base_url
        self._http = ProviderHttpBase(
            http_client=http_client, timeout=_TIMEOUT, user_agent=user_agent
        )

    async def __aenter__(self) -> SnapshotProvider:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._http.close()

    async def list_proposals(
        self, space_id: str, *, first: int = _MAX_PROPOSALS
    ) -> list[SnapshotProposal]:
        """Return proposals for *space_id*, newest first.

        Raises `SnapshotError` on any failure — request, HTTP status,
        GraphQL error, or an unparseable body — so the caller can tell
        "the space has no proposals" apart from "the fetch failed".
        """
        try:
            response = await self._http.client.post(
                self._base,
                json={"query": _QUERY, "variables": {"space": space_id, "first": first}},
            )
        except httpx.HTTPError as exc:
            raise SnapshotError(f"request failed: {exc}") from exc

        if not response.is_success:
            raise SnapshotError(f"HTTP {response.status_code}")

        try:
            data = response.json()
        except ValueError as exc:
            raise SnapshotError("unparseable response body") from exc

        if not isinstance(data, dict):
            raise SnapshotError("unexpected response shape")

        if data.get("errors"):
            raise SnapshotError(f"GraphQL error: {data['errors']}")

        proposals = (data.get("data") or {}).get("proposals")
        if not isinstance(proposals, list):
            raise SnapshotError("missing proposals in response")

        items: list[SnapshotProposal] = []
        for entry in proposals:
            if not isinstance(entry, dict):
                continue
            item = _parse_proposal(entry, space_id=space_id)
            if item is not None:
                items.append(item)
        return items


def _parse_proposal(entry: dict[str, Any], *, space_id: str) -> SnapshotProposal | None:
    proposal_id = entry.get("id")
    title = entry.get("title")
    state = entry.get("state")
    start = entry.get("start")
    end = entry.get("end")
    if not proposal_id or not title or not state or start is None or end is None:
        return None
    try:
        start_at = datetime.fromtimestamp(float(start), tz=UTC)
        end_at = datetime.fromtimestamp(float(end), tz=UTC)
    except (TypeError, ValueError):
        logger.info("snapshot: unparseable start/end for proposal=%r", proposal_id)
        return None

    body = entry.get("body") or ""
    summary = body if len(body) <= 280 else body[:279] + "…"

    return SnapshotProposal(
        id=str(proposal_id),
        title=str(title),
        summary=summary,
        state=str(state),
        start=start_at,
        end=end_at,
        link=f"https://snapshot.org/#/{space_id}/proposal/{proposal_id}",
    )
