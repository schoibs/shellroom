"""Server configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _read_int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default

    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc

    if parsed <= 0:
        raise ValueError(f"{name} must be greater than zero")

    return parsed


@dataclass(frozen=True, slots=True)
class ServerConfig:
    public_url: str = "http://localhost:8000"
    database_url: str = "sqlite:///./shellroom.db"
    room_id_length: int = 8
    max_users_per_room: int = 25
    max_active_rooms: int = 1000
    message_history_limit: int = 100

    @classmethod
    def from_env(cls) -> "ServerConfig":
        room_id_length = _read_int_env("SHELLROOM_ROOM_ID_LENGTH", 8)
        if not 6 <= room_id_length <= 12:
            raise ValueError("SHELLROOM_ROOM_ID_LENGTH must be between 6 and 12")

        public_url = os.getenv("SHELLROOM_PUBLIC_URL") or "http://localhost:8000"

        return cls(
            public_url=public_url.rstrip("/"),
            database_url=os.getenv("SHELLROOM_DATABASE_URL", "sqlite:///./shellroom.db"),
            room_id_length=room_id_length,
            max_users_per_room=_read_int_env("SHELLROOM_MAX_USERS_PER_ROOM", 25),
            max_active_rooms=_read_int_env("SHELLROOM_MAX_ACTIVE_ROOMS", 1000),
            message_history_limit=_read_int_env("SHELLROOM_MESSAGE_HISTORY_LIMIT", 100),
        )

    @property
    def websocket_base_url(self) -> str:
        if self.public_url.startswith("https://"):
            return "wss://" + self.public_url.removeprefix("https://")
        if self.public_url.startswith("http://"):
            return "ws://" + self.public_url.removeprefix("http://")
        return self.public_url
