# audr-mcp: an MCP server over your audr instance (AUD-632)

Stage 3 of [docs/llm-access.md](llm-access.md). `audr-mcp` is a thin client
that turns the read-only API tokens from [docs/api.md](api.md#api-tokens) and
the [LLM read surface](api.md#llm-read-surface) into tools an assistant like
Claude can call directly, instead of you pasting exports into a chat.

**Read the next paragraph before you mint a token for this.**

## The uncomfortable part, stated plainly

A token pasted into a *hosted* assistant — a web chat, a cloud connector —
hands that vendor your portfolio: composition, balances, and (unless you set
`redact_addresses`) your wallet addresses. **stdio is the transport this
document recommends**, because the token and every response stay on your own
machine; Claude Desktop and Claude Code launch `audr-mcp` as a local process
and talk to it over its stdin/stdout, never a socket. The streamable-HTTP
transport in §4 below exists for the cases where that is not possible, ships
disabled, and turning it on is a decision you make, not a default.

## 1. What it exposes

Seven tools, each one GET against your instance, and nothing that can
trigger a job or spend a metered provider budget:

| Tool | Reads |
|---|---|
| `get_portfolio` | `GET /api/v1/llm/context` — total value, top allocations, 24h/7d/30d change, latest headlines, unread alerts, freshness. Markdown, ~2k tokens. |
| `list_holdings` | `GET /api/v1/portfolio/holdings` — every wallet/asset line from the latest snapshot. |
| `get_asset` | `GET /api/v1/llm/asset/{ref}` — one asset by symbol, contract address, or UUID: quantity, value, price, a 30-day series, its news, its alerts. |
| `get_history` | `GET /api/v1/history` — value history for `period` (`24h`\|`7d`\|`30d`\|`90d`\|`1y`\|`all`). |
| `list_news` | `GET /api/v1/assets/{asset_id}/news` for one `asset` (resolved the same way as `get_asset`). |
| `list_alerts` | `GET /api/v1/alerts` — news-triggered alerts, optionally `unread_only`. |
| `get_status` | `GET /api/v1/status` — DB reachability, worker heartbeat, recent job outcomes. |

Plus one resource, `audr://portfolio/current`, the same compact snapshot as
`get_portfolio` for clients that attach resources rather than call tools.

What is **not** here on purpose: nothing that starts a balance scan, a quote
refresh, or any other job. Those spend your RPC/provider budget, and an
assistant should not be the thing that decides to spend it — see
[docs/llm-access.md §4](llm-access.md#4-stage-3--an-mcp-server).

## 2. Mint a token

Settings → API tokens → New token. Give it a name, accept the 90-day default
expiry (or pick your own), and copy the value shown — it is shown exactly
once. If you intend to ever use the HTTP transport in §4, turn on
`redact_addresses` for that token; stdio tokens that never leave your machine
don't need it, but it costs nothing to leave on either. Full reference:
[docs/api.md §API tokens](api.md#api-tokens).

## 3. stdio — Claude Desktop and Claude Code

Install the package where Claude will launch it from — the same machine
running Claude Desktop, or wherever `claude` (Claude Code) runs:

```bash
pip install audr-mcp
# or, without installing anything persistent:
uvx audr-mcp
```

Running from this checkout instead of a published package:

```bash
cd audr-mcp
uv sync
```

### Claude Desktop

Add an entry to `claude_desktop_config.json` (Settings → Developer → Edit
Config):

```json
{
  "mcpServers": {
    "audr": {
      "command": "uvx",
      "args": ["audr-mcp"],
      "env": {
        "AUDR_BASE_URL": "http://localhost:80",
        "AUDR_TOKEN": "audr_pat_..."
      }
    }
  }
}
```

Restart Claude Desktop. `audr` should appear as a connected MCP server with
seven tools.

### Claude Code

```bash
claude mcp add audr --env AUDR_BASE_URL=http://localhost:80 --env AUDR_TOKEN=audr_pat_... -- uvx audr-mcp
```

(Running from the checkout instead: replace `-- uvx audr-mcp` with
`-- uv run --directory /path/to/audr/audr-mcp audr-mcp`.)

### Configuration

| Variable | Required | Meaning |
|---|---|---|
| `AUDR_BASE_URL` | yes | Origin of your audr instance, e.g. `http://localhost:80`. |
| `AUDR_TOKEN` | yes | The token from §2. |
| `AUDR_TIMEOUT_SECONDS` | no (default `10`) | Per-request HTTP timeout. |

## 4. streamable HTTP — opt-in, off by default

Only for a hosted connector that cannot launch a local stdio process. Read
§"The uncomfortable part" above again before using this.

Disabled on two independent axes:

- `compose.mcp.yaml` is never included by `compose.yaml` — you have to name
  it explicitly.
- Even running, the process itself checks `MCP_ENABLED` and refuses to bind
  unless it is exactly `true`.

```bash
# .env: set AUDR_MCP_TOKEN to a token minted with redact_addresses on,
# and MCP_ENABLED=true
docker compose -f compose.yaml -f compose.mcp.yaml up -d --build mcp
```

The service binds to `127.0.0.1:8800` on the host by default (see
`compose.mcp.yaml`) — reaching it from anywhere else means you deliberately
widened that bind or put a reverse proxy in front of it, which is also where
you would terminate TLS and add whatever access control the hosted connector
needs on top of the bearer token.

## 5. Troubleshooting

| Symptom | Likely cause |
|---|---|
| `401` from every tool | Token revoked, expired, or mistyped. Mint a new one. |
| `403` from every tool | `AUDR_BASE_URL` does not point at `/api/v1` under the hood — double-check it has no trailing path. |
| Claude shows the server but 0 tools | `AUDR_BASE_URL` or `AUDR_TOKEN` env vars are missing from the client config — `audr-mcp` exits immediately rather than starting half-configured. |
| `get_asset`/`list_news` 404 | The `ref`/`asset` you passed does not resolve to a held or catalogued asset — try the symbol exactly as shown in `get_portfolio`'s `top_allocations`. |
