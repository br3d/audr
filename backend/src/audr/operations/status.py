"""System status primitives for migration, key, and worker health (T020)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ComponentStatus(str, Enum):
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
    active_workers: int = 0
    status: ComponentStatus = ComponentStatus.UNKNOWN


@dataclass
class SystemStatus:
    migration: MigrationStatus = field(default_factory=MigrationStatus)
    key: KeyStatus = field(default_factory=KeyStatus)
    worker: WorkerStatus = field(default_factory=WorkerStatus)

    @property
    def healthy(self) -> bool:
        return (
            self.migration.status == ComponentStatus.OK
            and self.key.status == ComponentStatus.OK
        )

    @property
    def ready(self) -> bool:
        return self.healthy and self.migration.up_to_date
