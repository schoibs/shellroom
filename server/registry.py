"""Room registry backed by SQLite room metadata."""

from __future__ import annotations

from asyncio import Lock
from datetime import UTC, datetime

from server.db.storage import RoomStorage
from server.model import ClientConnection, RuntimeRoom, StoredRoom
from server.utils import generate_room_id


class RoomRegistryError(Exception):
    """Base class for room registry errors."""


class RoomLimitExceeded(RoomRegistryError):
    """Raised when the server has reached the active room limit."""


class RoomIdGenerationFailed(RoomRegistryError):
    """Raised when a unique room ID cannot be generated."""


class RoomNotFound(RoomRegistryError):
    """Raised when a room does not exist."""


class RoomClosed(RoomRegistryError):
    """Raised when a room is no longer joinable."""


class RoomFull(RoomRegistryError):
    """Raised when a room has reached its user limit."""


class ClientAlreadyJoined(RoomRegistryError):
    """Raised when a client ID is already present in a room."""


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

    async def get_or_create_runtime_room(self, room_id: str) -> tuple[StoredRoom, RuntimeRoom]:
        async with self._lock:
            stored_room = await self.storage.get_room(room_id)
            if stored_room is None:
                raise RoomNotFound("Room does not exist.")
            if stored_room.status != "active" or stored_room.closed_at is not None:
                raise RoomClosed("Room is closed.")

            runtime_room = self._active_rooms.get(room_id)
            if runtime_room is None:
                runtime_room = RuntimeRoom(id=room_id)
                self._active_rooms[room_id] = runtime_room

            return stored_room, runtime_room

    async def add_client(
        self,
        room_id: str,
        client_id: str,
        display_name: str,
        websocket: object,
    ) -> tuple[RuntimeRoom, ClientConnection, list[ClientConnection], list[ClientConnection]]:
        stored_room, runtime_room = await self.get_or_create_runtime_room(room_id)

        async with runtime_room.lock:
            if client_id in runtime_room.clients:
                raise ClientAlreadyJoined("Client is already connected to the room.")
            if len(runtime_room.clients) >= stored_room.max_users:
                raise RoomFull("Room is full.")

            existing_clients = list(runtime_room.clients.values())
            client = ClientConnection(
                client_id=client_id,
                display_name=display_name,
                websocket=websocket,
                joined_at=datetime.now(UTC),
            )
            runtime_room.clients[client_id] = client
            current_clients = list(runtime_room.clients.values())

        return runtime_room, client, existing_clients, current_clients

    async def remove_client(
        self,
        room_id: str,
        client_id: str,
    ) -> tuple[ClientConnection | None, list[ClientConnection]]:
        runtime_room = self._active_rooms.get(room_id)
        if runtime_room is None:
            return None, []

        async with runtime_room.lock:
            client = runtime_room.clients.pop(client_id, None)
            remaining_clients = list(runtime_room.clients.values())

        return client, remaining_clients

    async def _generate_unique_room_id(self) -> str:
        for _ in range(32):
            room_id = generate_room_id(self.room_id_length)
            if await self.storage.get_room(room_id) is None:
                return room_id

        raise RoomIdGenerationFailed("Unable to generate a unique room ID.")
