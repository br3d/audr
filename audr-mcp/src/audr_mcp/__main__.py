"""Entrypoint for audr-mcp (AUD-632).

stdio is the default and recommended transport: Claude Desktop and Claude
Code launch this process locally and the token never leaves the operator's
machine (docs/mcp.md). The streamable-HTTP transport is opt-in twice over —
it lives in a compose file (compose.mcp.yaml) nothing else includes, and this
entrypoint itself refuses to bind it unless MCP_ENABLED=true.
"""

from __future__ import annotations

import argparse
import sys

from audr_mcp.config import ConfigError, load_config
from audr_mcp.server import build_server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="audr-mcp")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="stdio (default, recommended) or streamable-http (opt-in; see docs/mcp.md)",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"audr-mcp: {exc}", file=sys.stderr)
        return 1

    server = build_server(config)

    if args.transport == "http":
        if not config.mcp_enabled:
            print(
                'audr-mcp: MCP_ENABLED is not "true" — refusing to start the '
                "streamable-HTTP transport. Set MCP_ENABLED=true if you have "
                "deliberately chosen to run a hosted connector; stdio needs no "
                "such flag. See docs/mcp.md.",
                file=sys.stderr,
            )
            return 1
        server.run(transport="streamable-http", host=config.http_host, port=config.http_port)
    else:
        server.run(transport="stdio")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
