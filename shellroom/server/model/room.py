"""Room models for persisted metadata and runtime state."""

from __future__ import annotations

from asyncio import Lock
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class StoredRoom:
    id: str
    created_at: datetime
    last_active_at: datetime
    closed_at: datetime | None
    max_users: int
    status: str


@dataclass(frozen=True, slots=True)
class Message:
    id: str
    room_id: str
    client_id: str
    display_name: str
    text: str
    created_at: datetime


@dataclass(slots=True)
class ClientConnection:
    client_id: str
    display_name: str
    websocket: Any
    joined_at: datetime
    last_seen_at: datetime
    status: str = "online"


@dataclass(slots=True)
class RuntimeRoom:
    id: str
    clients: dict[str, ClientConnection] = field(default_factory=dict)
    typing_users: dict[str, datetime] = field(default_factory=dict)
    lock: Lock = field(default_factory=Lock)
