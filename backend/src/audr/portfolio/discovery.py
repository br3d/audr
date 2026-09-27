"""Token discovery — chunked, resumable scanning (T039, not yet implemented).

Stubs are present so tests can be collected.  Full implementation lands in T039.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class DiscoveryCandidate:
    token_address: str
    source: str


@dataclass
class DiscoveryResult:
    candidates: list[DiscoveryCandidate] = field(default_factory=list)
    checkpoint: dict[str, Any] | None = None


async def discover_tokens(
    session: AsyncSession,
    *,
    wallet_address: str,
    use_catalog: bool,
    manual_addresses: list[str],
    checkpoint: dict[str, Any] | None = None,
) -> DiscoveryResult:
    raise NotImplementedError("T039: discovery worker not yet implemented")


async def save_discovery_checkpoint(
    session: AsyncSession,
    *,
    wallet_address: str,
    checkpoint: dict[str, Any],
) -> None:
    raise NotImplementedError("T039: discovery worker not yet implemented")


async def get_discovery_checkpoint(
    session: AsyncSession,
    *,
    wallet_address: str,
) -> dict[str, Any] | None:
    raise NotImplementedError("T039: discovery worker not yet implemented")
