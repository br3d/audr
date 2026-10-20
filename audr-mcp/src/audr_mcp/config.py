"""Environment configuration for audr-mcp (AUD-632).

Everything the server needs comes from the environment, not a CLI flag or a
config file: the operator's `claude_desktop_config.json` / `claude mcp add`
entry sets `env` once and that is the only place the token lives (docs/mcp.md).
"""

from __future__ import annotations

import dataclasses
import os

_DEFAULT_TIMEOUT_SECONDS = 10.0
_DEFAULT_HTTP_HOST = "127.0.0.1"
_DEFAULT_HTTP_PORT = 8800


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


@dataclasses.dataclass(frozen=True)
class Config:
    base_url: str
    token: str
    timeout_seconds: float
    # Second, independent guard on the streamable-HTTP transport, on top of
    # it living in an opt-in compose file (compose.mcp.yaml) that is never
    # included by default. See docs/mcp.md §"Why HTTP is off by default".
    mcp_enabled: bool
    http_host: str
    http_port: int


def load_config(env: dict[str, str] | None = None) -> Config:
    source = env if env is not None else os.environ

    base_url = source.get("AUDR_BASE_URL", "").strip()
    if not base_url:
        raise ConfigError(
            "AUDR_BASE_URL is required — the origin of your audr instance, e.g. http://localhost:80"
        )

    token = source.get("AUDR_TOKEN", "").strip()
    if not token:
        raise ConfigError(
            "AUDR_TOKEN is required — mint a read-only API token from the audr "
            "Settings page and paste it here. It is never sent anywhere but "
            "your own audr instance."
        )

    timeout_raw = source.get("AUDR_TIMEOUT_SECONDS", str(_DEFAULT_TIMEOUT_SECONDS))
    try:
        timeout_seconds = float(timeout_raw)
    except ValueError as exc:
        raise ConfigError("AUDR_TIMEOUT_SECONDS must be a number") from exc

    mcp_enabled = source.get("MCP_ENABLED", "false").strip().lower() == "true"
    http_host = source.get("MCP_HTTP_HOST", _DEFAULT_HTTP_HOST).strip()

    port_raw = source.get("MCP_HTTP_PORT", str(_DEFAULT_HTTP_PORT))
    try:
        http_port = int(port_raw)
    except ValueError as exc:
        raise ConfigError("MCP_HTTP_PORT must be an integer") from exc

    return Config(
        base_url=base_url.rstrip("/"),
        token=token,
        timeout_seconds=timeout_seconds,
        mcp_enabled=mcp_enabled,
        http_host=http_host,
        http_port=http_port,
    )
