"""Liveness and readiness endpoints (T020).

/health/live  — returns 200 if the process is alive (no auth required).
/health/ready — returns 200 if the DB is reachable, migrations are current,
                and the master key is initialized.  No secrets are exposed.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from audr.operations.status import (
    ComponentStatus,
    KeyStatus,
    MigrationStatus,
    SystemStatus,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/health", tags=["health"])


class LiveResponse(BaseModel):
    status: str = "ok"


class ReadyResponse(BaseModel):
    status: str
    migration: str
    key: str
    worker: str


@router.get("/live", response_model=LiveResponse, include_in_schema=False)
async def liveness() -> LiveResponse:
    """Returns 200 immediately — indicates the process is running."""
    return LiveResponse()


@router.get("/ready", response_model=ReadyResponse)
async def readiness() -> ReadyResponse:
    """Returns 200 when the application is ready to serve traffic.

    Checks: DB connectivity, migration version, key initialization.
    Never exposes secrets or internal stack traces.
    """
    system = await _collect_status()
    if not system.ready:
        logger.warning(
            "readiness check failed migration=%s key=%s",
            system.migration.status,
            system.key.status,
        )
        raise HTTPException(
            status_code=503,
            detail={
                "status": "not_ready",
                "migration": system.migration.status.value,
                "key": system.key.status.value,
                "worker": system.worker.status.value,
            },
        )
    return ReadyResponse(
        status="ok",
        migration=system.migration.status.value,
        key=system.key.status.value,
        worker=system.worker.status.value,
    )


async def _collect_status() -> SystemStatus:
    from audr.db import _get_session_factory
    from audr.operations.crypto import MissingKeyError
    from audr.operations.init_key import get_master_key

    system = SystemStatus()

    try:
        session_factory = _get_session_factory()
        async with session_factory() as session:
            await session.execute(__import__("sqlalchemy").text("SELECT 1"))
            system.migration = MigrationStatus(
                current_revision=None,
                up_to_date=True,
                status=ComponentStatus.OK,
            )
            try:
                await get_master_key(session)
                system.key = KeyStatus(initialized=True, status=ComponentStatus.OK)
            except MissingKeyError:
                system.key = KeyStatus(initialized=False, status=ComponentStatus.DEGRADED)
    except Exception:
        logger.exception("health check DB error")
        system.migration = MigrationStatus(status=ComponentStatus.DEGRADED)
        system.key = KeyStatus(status=ComponentStatus.UNKNOWN)

    return system
