"""Room models for persisted metadata and runtime state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class StoredRoom:
    id: str
    created_at: datetime
    last_active_at: datetime
    closed_at: datetime | None
    max_users: int
    status: str


@dataclass(slots=True)
class RuntimeRoom:
    id: str
