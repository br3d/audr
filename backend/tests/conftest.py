"""
Root conftest — shared pytest fixtures and marker declarations.

Markers (also declared in pyproject.toml [tool.pytest.ini_options]):
  @pytest.mark.unit        — fast, no I/O, no external services
  @pytest.mark.integration — requires live PostgreSQL
  @pytest.mark.contract    — verifies provider-interface contracts against fixtures
"""

import pytest


@pytest.fixture()
def anyio_backend() -> str:
    return "asyncio"
