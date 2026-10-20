"""MCP tool and resource definitions for audr-mcp (AUD-632).

Every tool wraps one or two chained calls to `AudrClient.get` — the client
has no mutating method, so nothing here can trigger a job, spend provider
budget, or write anything to the instance. See docs/mcp.md for the mapping
from each tool to the audr route it reads, and docs/llm-access.md §4 for why
job triggers are deliberately absent.
"""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from audr_mcp.client import AudrClient
from audr_mcp.config import Config

_INSTRUCTIONS = (
    "Read-only view of a single-owner, self-hosted audr Ethereum portfolio "
    "instance. Every tool here is a GET; none can trigger a background job, "
    "spend a metered provider budget, or write anything. Figures reflect the "
    "last published snapshot or cache, not a live chain read — always surface "
    "a response's `caveats` rather than stating a number as current fact."
)


def build_server(config: Config, client: AudrClient | None = None) -> MCPServer:
    """Build the MCPServer instance, wiring each tool to *client* (or one
    freshly constructed from *config*)."""
    audr = (
        client
        if client is not None
        else AudrClient(config.base_url, config.token, timeout_seconds=config.timeout_seconds)
    )

    server = MCPServer(name="audr-mcp", instructions=_INSTRUCTIONS)

    @server.tool()
    async def get_portfolio() -> str:
        """Portfolio snapshot: total value, priced subtotal, quality flags,
        top allocations, 24h/7d/30d change, latest headlines, unread alerts,
        and per-subsystem freshness. Kept under ~2k tokens by design."""
        response = await audr.get("/api/v1/llm/context", accept="text/markdown")
        return response.text

    @server.tool()
    async def list_holdings() -> str:
        """Every holding (one row per wallet/asset) from the latest published
        valuation snapshot, with exact decimal quantity and USD value where
        priced. Unpriced or unscanned values are null, never zero."""
        response = await audr.get("/api/v1/portfolio/holdings")
        return response.text

    @server.tool()
    async def get_asset(
        ref: Annotated[
            str, Field(description="Symbol (e.g. 'ETH'), contract address, or asset UUID")
        ],
    ) -> str:
        """One asset: quantity, value, price, portfolio share, a 30-day value
        series, its cached news, and its alerts."""
        response = await audr.get(f"/api/v1/llm/asset/{ref}", accept="text/markdown")
        return response.text

    @server.tool()
    async def get_history(
        period: Annotated[str, Field(description="One of: 24h, 7d, 30d, 90d, 1y, all")] = "7d",
    ) -> str:
        """Portfolio value history for the given period. Gap markers show
        timeline discontinuities explicitly rather than smoothing over them."""
        response = await audr.get("/api/v1/history", params={"period": period})
        return response.text

    @server.tool()
    async def list_news(
        asset: Annotated[str, Field(description="Symbol, contract address, or asset UUID")],
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> str:
        """Cached news for one asset, newest first. `asset` is resolved the
        same way get_asset resolves `ref`."""
        asset_response = await audr.get(f"/api/v1/llm/asset/{asset}")
        asset_id = asset_response.json()["asset_id"]
        news_response = await audr.get(f"/api/v1/assets/{asset_id}/news", params={"limit": limit})
        return news_response.text

    @server.tool()
    async def list_alerts(
        unread_only: Annotated[bool, Field(description="Only return unread alerts")] = False,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> str:
        """News-triggered alerts across the whole portfolio, unread first,
        then newest first."""
        response = await audr.get("/api/v1/alerts", params={"unread": unread_only, "limit": limit})
        return response.text

    @server.tool()
    async def get_status() -> str:
        """Operational status: database reachability, worker heartbeat, and
        recent job outcomes."""
        response = await audr.get("/api/v1/status")
        return response.text

    @server.resource("audr://portfolio/current", mime_type="text/markdown")
    async def portfolio_current() -> str:
        """The same compact snapshot as get_portfolio, exposed as an
        attachable resource so a client can pull it in without a tool call."""
        response = await audr.get("/api/v1/llm/context", accept="text/markdown")
        return response.text

    return server
