import pytest

from audr_mcp.config import ConfigError, load_config


def test_requires_base_url() -> None:
    with pytest.raises(ConfigError, match="AUDR_BASE_URL"):
        load_config({"AUDR_TOKEN": "audr_pat_x"})


def test_requires_token() -> None:
    with pytest.raises(ConfigError, match="AUDR_TOKEN"):
        load_config({"AUDR_BASE_URL": "http://localhost:80"})


def test_defaults() -> None:
    config = load_config({"AUDR_BASE_URL": "http://localhost:80/", "AUDR_TOKEN": "audr_pat_x"})
    assert config.base_url == "http://localhost:80"
    assert config.token == "audr_pat_x"
    assert config.timeout_seconds == 10.0
    assert config.mcp_enabled is False
    assert config.http_host == "127.0.0.1"
    assert config.http_port == 8800


def test_mcp_enabled_requires_exact_true() -> None:
    assert (
        load_config(
            {"AUDR_BASE_URL": "http://localhost:80", "AUDR_TOKEN": "t", "MCP_ENABLED": "1"}
        ).mcp_enabled
        is False
    )
    assert (
        load_config(
            {"AUDR_BASE_URL": "http://localhost:80", "AUDR_TOKEN": "t", "MCP_ENABLED": "true"}
        ).mcp_enabled
        is True
    )
    assert (
        load_config(
            {"AUDR_BASE_URL": "http://localhost:80", "AUDR_TOKEN": "t", "MCP_ENABLED": "True"}
        ).mcp_enabled
        is True
    )


def test_rejects_non_numeric_timeout() -> None:
    with pytest.raises(ConfigError, match="AUDR_TIMEOUT_SECONDS"):
        load_config(
            {
                "AUDR_BASE_URL": "http://localhost:80",
                "AUDR_TOKEN": "t",
                "AUDR_TIMEOUT_SECONDS": "soon",
            }
        )


def test_rejects_non_integer_http_port() -> None:
    with pytest.raises(ConfigError, match="MCP_HTTP_PORT"):
        load_config(
            {
                "AUDR_BASE_URL": "http://localhost:80",
                "AUDR_TOKEN": "t",
                "MCP_HTTP_PORT": "not-a-port",
            }
        )
