"""Room registry backed by SQLite room metadata."""

from __future__ import annotations

from asyncio import Lock
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from shellroom.server.db.storage import RoomStorage
from shellroom.server.model import ClientConnection, RuntimeRoom, StoredRoom
from shellroom.server.utils import generate_room_id


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


@dataclass(frozen=True, slots=True)
class UserStatusChange:
    room_id: str
    client: ClientConnection
    clients: list[ClientConnection]


@dataclass(frozen=True, slots=True)
class TypingUsersChange:
    room_id: str
    clients: list[ClientConnection]
    typing_clients: list[ClientConnection]


@dataclass(frozen=True, slots=True)
class PresenceSweepResult:
    status_changes: list[UserStatusChange]
    typing_changes: list[TypingUsersChange]


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
            now = datetime.now(UTC)
            client = ClientConnection(
                client_id=client_id,
                display_name=display_name,
                websocket=websocket,
                joined_at=now,
                last_seen_at=now,
                status="online",
            )
            runtime_room.clients[client_id] = client
            current_clients = list(runtime_room.clients.values())

        return runtime_room, client, existing_clients, current_clients

    async def remove_client(
        self,
        room_id: str,
        client_id: str,
    ) -> tuple[ClientConnection | None, list[ClientConnection], list[ClientConnection] | None]:
        runtime_room = self._active_rooms.get(room_id)
        if runtime_room is None:
            return None, [], None

        async with runtime_room.lock:
            client = runtime_room.clients.pop(client_id, None)
            was_typing = runtime_room.typing_users.pop(client_id, None) is not None
            remaining_clients = list(runtime_room.clients.values())
            typing_clients = self._typing_clients(runtime_room) if was_typing else None

        return client, remaining_clients, typing_clients

    async def list_clients(self, room_id: str) -> list[ClientConnection]:
        runtime_room = self._active_rooms.get(room_id)
        if runtime_room is None:
            return []

        async with runtime_room.lock:
            return list(runtime_room.clients.values())

    async def list_typing_clients(self, room_id: str) -> list[ClientConnection]:
        runtime_room = self._active_rooms.get(room_id)
        if runtime_room is None:
            return []

        async with runtime_room.lock:
            return self._typing_clients(runtime_room)

    async def mark_client_active(
        self,
        room_id: str,
        client_id: str,
    ) -> tuple[ClientConnection | None, list[ClientConnection], bool]:
        runtime_room = self._active_rooms.get(room_id)
        if runtime_room is None:
            return None, [], False

        async with runtime_room.lock:
            client = runtime_room.clients.get(client_id)
            if client is None:
                return None, [], False

            was_idle = client.status == "idle"
            client.last_seen_at = datetime.now(UTC)
            client.status = "online"
            return client, list(runtime_room.clients.values()), was_idle

    async def set_typing(
        self,
        room_id: str,
        client_id: str,
        is_typing: bool,
    ) -> tuple[list[ClientConnection], list[ClientConnection] | None]:
        runtime_room = self._active_rooms.get(room_id)
        if runtime_room is None:
            return [], None

        async with runtime_room.lock:
            if client_id not in runtime_room.clients:
                return list(runtime_room.clients.values()), None

            previous_typing_ids = self._typing_client_ids(runtime_room)
            if is_typing:
                runtime_room.typing_users[client_id] = datetime.now(UTC)
            else:
                runtime_room.typing_users.pop(client_id, None)

            typing_ids = self._typing_client_ids(runtime_room)
            if typing_ids == previous_typing_ids:
                return list(runtime_room.clients.values()), None

            return list(runtime_room.clients.values()), self._typing_clients(runtime_room)

    async def sweep_presence(
        self,
        idle_timeout_seconds: int,
        typing_timeout_seconds: int,
    ) -> PresenceSweepResult:
        now = datetime.now(UTC)
        idle_timeout = timedelta(seconds=idle_timeout_seconds)
        typing_timeout = timedelta(seconds=typing_timeout_seconds)
        status_changes: list[UserStatusChange] = []
        typing_changes: list[TypingUsersChange] = []

        async with self._lock:
            runtime_rooms = list(self._active_rooms.values())

        for runtime_room in runtime_rooms:
            async with runtime_room.lock:
                previous_typing_ids = self._typing_client_ids(runtime_room)
                for typing_client_id, last_typed_at in list(runtime_room.typing_users.items()):
                    if (
                        typing_client_id not in runtime_room.clients
                        or now - last_typed_at >= typing_timeout
                    ):
                        runtime_room.typing_users.pop(typing_client_id, None)

                typing_ids = self._typing_client_ids(runtime_room)
                clients = list(runtime_room.clients.values())
                if typing_ids != previous_typing_ids:
                    typing_changes.append(
                        TypingUsersChange(
                            room_id=runtime_room.id,
                            clients=clients,
                            typing_clients=self._typing_clients(runtime_room),
                        )
                    )

                for client in clients:
                    if client.status == "online" and now - client.last_seen_at >= idle_timeout:
                        client.status = "idle"
                        status_changes.append(
                            UserStatusChange(
                                room_id=runtime_room.id,
                                client=client,
                                clients=clients,
                            )
                        )

        return PresenceSweepResult(
            status_changes=status_changes,
            typing_changes=typing_changes,
        )

    async def _generate_unique_room_id(self) -> str:
        for _ in range(32):
            room_id = generate_room_id(self.room_id_length)
            if await self.storage.get_room(room_id) is None:
                return room_id

        raise RoomIdGenerationFailed("Unable to generate a unique room ID.")

    def _typing_client_ids(self, runtime_room: RuntimeRoom) -> list[str]:
        return [
            client_id
            for client_id in runtime_room.typing_users
            if client_id in runtime_room.clients
        ]

    def _typing_clients(self, runtime_room: RuntimeRoom) -> list[ClientConnection]:
        return [
            runtime_room.clients[client_id]
            for client_id in self._typing_client_ids(runtime_room)
        ]
