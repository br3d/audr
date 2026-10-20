"""End-to-end AC check: the server actually comes up over stdio and answers
tools/list — not just that build_server() returns the right Python object.
"""

from __future__ import annotations

import os
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

_EXPECTED_TOOL_NAMES = {
    "get_portfolio",
    "list_holdings",
    "get_asset",
    "get_history",
    "list_news",
    "list_alerts",
    "get_status",
}


async def test_server_starts_over_stdio_and_lists_tools() -> None:
    env = dict(os.environ)
    env["AUDR_BASE_URL"] = "http://localhost:80"
    env["AUDR_TOKEN"] = "audr_pat_test"

    params = StdioServerParameters(command=sys.executable, args=["-m", "audr_mcp"], env=env)

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert {t.name for t in tools.tools} == _EXPECTED_TOOL_NAMES

            resources = await session.list_resources()
            assert [str(r.uri) for r in resources.resources] == ["audr://portfolio/current"]
