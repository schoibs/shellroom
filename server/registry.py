"""Room registry backed by SQLite room metadata."""

from __future__ import annotations

from asyncio import Lock

from server.db.storage import RoomStorage
from server.model import RuntimeRoom, StoredRoom
from server.utils import generate_room_id


class RoomRegistryError(Exception):
    """Base class for room registry errors."""


class RoomLimitExceeded(RoomRegistryError):
    """Raised when the server has reached the active room limit."""


class RoomIdGenerationFailed(RoomRegistryError):
    """Raised when a unique room ID cannot be generated."""


class RoomRegistry:
    def __init__(
        self,
        storage: RoomStorage,
        room_id_length: int,
        max_active_rooms: int,
        max_users_per_room: int,
    ) -> None:
        self.storage = storage
        self.room_id_length = room_id_length
        self.max_active_rooms = max_active_rooms
        self.max_users_per_room = max_users_per_room
        self._active_rooms: dict[str, RuntimeRoom] = {}
        self._lock = Lock()

    async def active_room_count(self) -> int:
        return await self.storage.count_active_rooms()

    async def create_room(self) -> StoredRoom:
        async with self._lock:
            if await self.storage.count_active_rooms() >= self.max_active_rooms:
                raise RoomLimitExceeded("Maximum active room count reached.")

            room_id = await self._generate_unique_room_id()
            room = await self.storage.create_room(
                room_id=room_id,
                max_users=self.max_users_per_room,
            )
            self._active_rooms[room.id] = RuntimeRoom(id=room.id)
            return room

    async def get_room(self, room_id: str) -> StoredRoom | None:
        return await self.storage.get_room(room_id)

    def get_runtime_room(self, room_id: str) -> RuntimeRoom | None:
        return self._active_rooms.get(room_id)

    async def _generate_unique_room_id(self) -> str:
        for _ in range(32):
            room_id = generate_room_id(self.room_id_length)
            if await self.storage.get_room(room_id) is None:
                return room_id

        raise RoomIdGenerationFailed("Unable to generate a unique room ID.")
