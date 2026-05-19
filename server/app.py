"""FastAPI application for the ShellRoom server."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Body, FastAPI, HTTPException, WebSocket, status
from pydantic import BaseModel, ConfigDict, Field
from starlette.websockets import WebSocketDisconnect

from server.config import ServerConfig
from server.db.session import SQLiteDatabase
from server.db.storage import RoomStorage
from server.model import ClientConnection
from server.registry import (
    ClientAlreadyJoined,
    RoomClosed,
    RoomFull,
    RoomIdGenerationFailed,
    RoomLimitExceeded,
    RoomNotFound,
    RoomRegistry,
)


class CreateRoomRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    display_name: str | None = Field(default=None, min_length=1, max_length=24)


class CreateRoomResponse(BaseModel):
    room_id: str
    invite_url: str
    join_command: str
    websocket_url: str


class HealthResponse(BaseModel):
    status: str


class JoinEventError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


config = ServerConfig.from_env()
database = SQLiteDatabase(config.database_url)
room_storage = RoomStorage(database)
room_registry = RoomRegistry(
    storage=room_storage,
    room_id_length=config.room_id_length,
    max_active_rooms=config.max_active_rooms,
    max_users_per_room=config.max_users_per_room,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    database.connect()
    await room_storage.initialize()
    try:
        yield
    finally:
        await database.close()


app = FastAPI(title="ShellRoom", version="0.1.0", lifespan=lifespan)


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _server_event(room_id: str, event_type: str, payload: dict) -> dict:
    return {
        "type": event_type,
        "room_id": room_id,
        "timestamp": _utc_timestamp(),
        "payload": payload,
    }


def _client_presence(client: ClientConnection) -> dict:
    return {
        "client_id": client.client_id,
        "display_name": client.display_name,
        "status": "online",
    }


def _room_state_event(room_id: str, clients: list[ClientConnection]) -> dict:
    return _server_event(
        room_id=room_id,
        event_type="room_state",
        payload={
            "users": [_client_presence(client) for client in clients],
            "recent_messages": [],
        },
    )


def _user_joined_event(room_id: str, client: ClientConnection) -> dict:
    return _server_event(
        room_id=room_id,
        event_type="user_joined",
        payload={
            "client_id": client.client_id,
            "display_name": client.display_name,
        },
    )


def _user_left_event(room_id: str, client: ClientConnection) -> dict:
    return _server_event(
        room_id=room_id,
        event_type="user_left",
        payload={
            "client_id": client.client_id,
            "display_name": client.display_name,
        },
    )


def _error_event(room_id: str, code: str, message: str) -> dict:
    return _server_event(
        room_id=room_id,
        event_type="error",
        payload={
            "code": code,
            "message": message,
        },
    )


def _parse_join_event(room_id: str, event: object) -> tuple[str, str]:
    if not isinstance(event, dict):
        raise JoinEventError("INVALID_JOIN", "First WebSocket message must be a JSON object.")

    if event.get("type") != "join_room":
        raise JoinEventError("INVALID_JOIN", "First WebSocket message must be a join_room event.")

    if event.get("room_id") != room_id:
        raise JoinEventError("ROOM_MISMATCH", "Join event room_id must match the WebSocket path.")

    client_id = event.get("client_id")
    if not isinstance(client_id, str) or not client_id.strip():
        raise JoinEventError("INVALID_CLIENT_ID", "Join event requires a client_id.")
    client_id = client_id.strip()

    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        raise JoinEventError("INVALID_PAYLOAD", "Join event payload must be an object.")

    display_name = payload.get("display_name")
    if not isinstance(display_name, str) or not display_name.strip():
        raise JoinEventError("INVALID_DISPLAY_NAME", "Join event requires a display_name.")

    display_name = display_name.strip()
    if len(display_name) > 24:
        raise JoinEventError("INVALID_DISPLAY_NAME", "Display name must be 24 characters or fewer.")

    return client_id, display_name


async def _send_error(websocket: WebSocket, room_id: str, code: str, message: str) -> None:
    await websocket.send_json(_error_event(room_id, code, message))


async def _send_error_and_close(
    websocket: WebSocket,
    room_id: str,
    code: str,
    message: str,
) -> None:
    try:
        await _send_error(websocket, room_id, code, message)
    finally:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)


async def _broadcast(clients: list[ClientConnection], event: dict) -> None:
    for client in clients:
        try:
            await client.websocket.send_json(event)
        except Exception:
            pass


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.post("/rooms", response_model=CreateRoomResponse)
async def create_room(
    request: Annotated[CreateRoomRequest | None, Body()] = None,
) -> CreateRoomResponse:
    _ = request

    try:
        room = await room_registry.create_room()
    except RoomLimitExceeded as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Maximum active room count reached.",
        ) from exc
    except RoomIdGenerationFailed as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to create a room ID.",
        ) from exc

    return CreateRoomResponse(
        room_id=room.id,
        invite_url=f"{config.public_url}/join/{room.id}",
        join_command=f"shellroom join {room.id}",
        websocket_url=f"{config.websocket_base_url}/ws/{room.id}",
    )


@app.websocket("/ws/{room_id}")
async def websocket_room(websocket: WebSocket, room_id: str) -> None:
    await websocket.accept()

    joined_client_id: str | None = None
    try:
        # server expects client to send join event upon connecting
        try:
            join_event = await websocket.receive_json()
        except ValueError:
            await _send_error_and_close(
                websocket,
                room_id,
                "INVALID_JSON",
                "First WebSocket message must be valid JSON.",
            )
            return

        try:
            client_id, display_name = _parse_join_event(room_id, join_event)
            _runtime_room, client, existing_clients, current_clients = await room_registry.add_client(
                room_id=room_id,
                client_id=client_id,
                display_name=display_name,
                websocket=websocket,
            )
            
        except JoinEventError as exc:
            await _send_error_and_close(websocket, room_id, exc.code, exc.message)
            return
        except RoomNotFound:
            await _send_error_and_close(
                websocket,
                room_id,
                "ROOM_NOT_FOUND",
                "Room does not exist.",
            )
            return
        except RoomClosed:
            await _send_error_and_close(websocket, room_id, "ROOM_CLOSED", "Room is closed.")
            return
        except RoomFull:
            await _send_error_and_close(websocket, room_id, "ROOM_FULL", "Room is full.")
            return
        except ClientAlreadyJoined:
            await _send_error_and_close(
                websocket,
                room_id,
                "CLIENT_ALREADY_JOINED",
                "Client is already connected to the room.",
            )
            return

        joined_client_id = client.client_id
        await websocket.send_json(_room_state_event(room_id, current_clients))
        await _broadcast(existing_clients, _user_joined_event(room_id, client))

        while True:
            try:
                event = await websocket.receive_json()
            except ValueError:
                await _send_error(websocket, room_id, "INVALID_JSON", "Expected a JSON event.")
                continue

            event_type = event.get("type") if isinstance(event, dict) else "unknown"
            await _send_error(
                websocket,
                room_id,
                "UNSUPPORTED_EVENT",
                f"Event type '{event_type}' is not supported yet.",
            )
    except WebSocketDisconnect:
        pass
    finally:
        if joined_client_id is not None:
            left_client, remaining_clients = await room_registry.remove_client(
                room_id=room_id,
                client_id=joined_client_id,
            )
            if left_client is not None:
                await _broadcast(remaining_clients, _user_left_event(room_id, left_client))
