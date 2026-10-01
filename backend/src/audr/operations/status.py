"""System status primitives for migration, key, and worker health (T020)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ComponentStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


@dataclass
class MigrationStatus:
    current_revision: str | None = None
    up_to_date: bool = False
    status: ComponentStatus = ComponentStatus.UNKNOWN


@dataclass
class KeyStatus:
    initialized: bool = False
    status: ComponentStatus = ComponentStatus.UNKNOWN


@dataclass
class WorkerStatus:
    # idle | running | stopped | unknown — the worker-process heartbeat
    # vocabulary (see audr.jobs.store.get_worker_heartbeat), not a
    # ComponentStatus: a "stopped" worker is a known, reportable fact, not an
    # UNKNOWN one, and readiness never gated on worker liveness anyway.
    status: str = "unknown"


@dataclass
class CatalogStatus:
    # DEGRADED means no catalog_version row has entries yet — ERC-20 discovery
    # finds zero candidates even though the worker is otherwise healthy
    # (AUD-357: a prior silent catalog-import failure left this invisible).
    entry_count: int = 0
    status: ComponentStatus = ComponentStatus.UNKNOWN


@dataclass
class SystemStatus:
    migration: MigrationStatus = field(default_factory=MigrationStatus)
    key: KeyStatus = field(default_factory=KeyStatus)
    worker: WorkerStatus = field(default_factory=WorkerStatus)
    catalog: CatalogStatus = field(default_factory=CatalogStatus)

    @property
    def healthy(self) -> bool:
        return (
            self.migration.status == ComponentStatus.OK
            and self.key.status == ComponentStatus.OK
        )

    @property
    def ready(self) -> bool:
        return self.healthy and self.migration.up_to_date
