"""Shared pytest fixtures and configuration for all test suites."""

import pytest


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"
