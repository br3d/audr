"""FastAPI route for the supported-networks list (AUD-335 / SD-1).

Release-1 scope is Ethereum mainnet only (chain_id=1); this is a static,
non-database-backed list per the internal release-1 HTTP API contract.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from audr.api.auth import _require_session
from audr.auth.models import Session

router = APIRouter(prefix="/api/v1")


class NetworkOut(BaseModel):
    chain_id: int
    name: str
    native_symbol: str


_NETWORKS: list[NetworkOut] = [
    NetworkOut(chain_id=1, name="Ethereum", native_symbol="ETH"),
]


@router.get("/networks", response_model=list[NetworkOut])
async def list_networks(
    _session: Annotated[Session, Depends(_require_session)],
) -> list[NetworkOut]:
    return _NETWORKS
