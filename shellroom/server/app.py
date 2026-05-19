"""FastAPI application for the ShellRoom server."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Body, FastAPI, HTTPException, WebSocket, status
from pydantic import BaseModel, ConfigDict, Field
from starlette.websockets import WebSocketDisconnect

from shellroom.server.config import ServerConfig
from shellroom.server.db.session import SQLiteDatabase
from shellroom.server.db.storage import RoomStorage
from shellroom.server.model import ClientConnection, Message
from shellroom.server.registry import (
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


class ClientEventError(Exception):
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

    # starts a background asyncio task
    presence_task = asyncio.create_task(_presence_sweep()) 
    try:
        yield
    finally:
        # on shutdown, stop bg task, waits for it to acknowledge cancellation, ignores the normal cancellation exception, then closes the database
        presence_task.cancel()
        with suppress(asyncio.CancelledError):
            await presence_task
        await database.close()


app = FastAPI(title="ShellRoom", version="0.1.0", lifespan=lifespan)


def _format_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _utc_timestamp() -> str:
    return _format_timestamp(datetime.now(UTC))


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
        "status": client.status,
    }


def _message_payload(message: Message) -> dict:
    return {
        "message_id": message.id,
        "client_id": message.client_id,
        "display_name": message.display_name,
        "text": message.text,
        "timestamp": _format_timestamp(message.created_at),
    }


def _room_state_event(
    room_id: str,
    clients: list[ClientConnection],
    recent_messages: list[Message],
) -> dict:
    return _server_event(
        room_id=room_id,
        event_type="room_state",
        payload={
            "users": [_client_presence(client) for client in clients],
            "recent_messages": [_message_payload(message) for message in recent_messages],
        },
    )


def _chat_message_event(room_id: str, message: Message) -> dict:
    return {
        "type": "chat_message",
        "room_id": room_id,
        "timestamp": _format_timestamp(message.created_at),
        "payload": {
            "message_id": message.id,
            "client_id": message.client_id,
            "display_name": message.display_name,
            "text": message.text,
        },
    }


def _user_joined_event(room_id: str, client: ClientConnection) -> dict:
    return _server_event(
        room_id=room_id,
        event_type="user_joined",
        payload={
            "client_id": client.client_id,
            "display_name": client.display_name,
            "status": client.status,
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


def _user_status_event(room_id: str, event_type: str, client: ClientConnection) -> dict:
    return _server_event(
        room_id=room_id,
        event_type=event_type,
        payload={
            "client_id": client.client_id,
            "display_name": client.display_name,
            "status": client.status,
        },
    )


def _typing_users_event(room_id: str, typing_clients: list[ClientConnection]) -> dict:
    return _server_event(
        room_id=room_id,
        event_type="typing_users",
        payload={
            "users": [client.display_name for client in typing_clients],
            "client_ids": [client.client_id for client in typing_clients],
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


def _has_control_characters(text: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in text)


def _parse_client_event(
    room_id: str,
    joined_client_id: str,
    event: object,
) -> tuple[str, dict]:
    if not isinstance(event, dict):
        raise ClientEventError("INVALID_EVENT", "Expected a JSON object.")

    event_type = event.get("type")
    if event_type not in {"chat_message", "typing_start", "typing_stop"}:
        raise ClientEventError("UNSUPPORTED_EVENT", f"Event type '{event_type}' is not supported yet.")

    if event.get("room_id") != room_id:
        raise ClientEventError("ROOM_MISMATCH", "Event room_id must match the WebSocket path.")

    if event.get("client_id") != joined_client_id:
        raise ClientEventError("CLIENT_MISMATCH", "Event client_id must match the joined client.")

    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        raise ClientEventError("INVALID_PAYLOAD", "Event payload must be an object.")

    return str(event_type), payload


def _parse_chat_message_payload(payload: dict) -> str:
    text = payload.get("text")
    if not isinstance(text, str):
        raise ClientEventError("INVALID_MESSAGE", "Message text is required.")

    text = text.rstrip("\r\n")
    if not text.strip():
        raise ClientEventError("INVALID_MESSAGE", "Message text cannot be empty.")
    if len(text) > 2000:
        raise ClientEventError("INVALID_MESSAGE", "Message text must be 2,000 characters or fewer.")
    if _has_control_characters(text):
        raise ClientEventError("INVALID_MESSAGE", "Message text cannot contain control characters.")

    return text


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


async def _broadcast_active_if_needed(
    room_id: str,
    client: ClientConnection | None,
    clients: list[ClientConnection],
    became_active: bool,
) -> None:
    if client is not None and became_active:
        await _broadcast(clients, _user_status_event(room_id, "user_active", client))


async def _broadcast_typing_if_changed(
    room_id: str,
    clients: list[ClientConnection],
    typing_clients: list[ClientConnection] | None,
) -> None:
    if typing_clients is not None:
        await _broadcast(clients, _typing_users_event(room_id, typing_clients))


async def _presence_sweep() -> None:
    """
    Checks for users who became idle or stopped typing on every second, then broadcasts those changes to connected clients
    """
    while True:
        await asyncio.sleep(1)
        sweep_result = await room_registry.sweep_presence(
            idle_timeout_seconds=config.idle_timeout_seconds,
            typing_timeout_seconds=config.typing_timeout_seconds,
        )

        for change in sweep_result.typing_changes:
            await _broadcast(
                change.clients,
                _typing_users_event(change.room_id, change.typing_clients),
            )

        for change in sweep_result.status_changes:
            await _broadcast(
                change.clients,
                _user_status_event(change.room_id, "user_idle", change.client),
            )


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
            await _send_error_and_close(websocket, room_id, "ROOM_NOT_FOUND", "Room does not exist.")
            return
        except RoomClosed:
            await _send_error_and_close(websocket, room_id, "ROOM_CLOSED", "Room is closed.")
            return
        except RoomFull:
            await _send_error_and_close(websocket, room_id, "ROOM_FULL", "Room is full.")
            return
        except ClientAlreadyJoined:
            await _send_error_and_close(websocket, room_id, "CLIENT_ALREADY_JOINED", "Client is already connected to the room.")
            return

        joined_client_id = client.client_id
        recent_messages = await room_storage.list_recent_messages(room_id=room_id, limit=config.message_history_limit)
        await websocket.send_json(_room_state_event(room_id, current_clients, recent_messages))
        typing_clients = await room_registry.list_typing_clients(room_id)
        if typing_clients:
            await websocket.send_json(_typing_users_event(room_id, typing_clients))
        await _broadcast(existing_clients, _user_joined_event(room_id, client))

        while True:
            try:
                event = await websocket.receive_json()
            except ValueError:
                await _send_error(websocket, room_id, "INVALID_JSON", "Expected a JSON event.")
                continue

            try:
                event_type, payload = _parse_client_event(room_id, joined_client_id, event)
            except ClientEventError as exc:
                await _send_error(websocket, room_id, exc.code, exc.message)
                continue

            if event_type == "typing_start":
                active_client, active_clients, became_active = await room_registry.mark_client_active(
                    room_id=room_id,
                    client_id=joined_client_id,
                )
                await _broadcast_active_if_needed(room_id, active_client, active_clients, became_active)

                clients, typing_update = await room_registry.set_typing(
                    room_id=room_id,
                    client_id=joined_client_id,
                    is_typing=True,
                )
                await _broadcast_typing_if_changed(room_id, clients, typing_update)
                continue

            if event_type == "typing_stop":
                active_client, active_clients, became_active = await room_registry.mark_client_active(
                    room_id=room_id,
                    client_id=joined_client_id,
                )
                await _broadcast_active_if_needed(room_id, active_client, active_clients, became_active)

                clients, typing_update = await room_registry.set_typing(
                    room_id=room_id,
                    client_id=joined_client_id,
                    is_typing=False,
                )
                await _broadcast_typing_if_changed(room_id, clients, typing_update)
                continue

            try:
                text = _parse_chat_message_payload(payload)
            except ClientEventError as exc:
                await _send_error(websocket, room_id, exc.code, exc.message)
                continue

            active_client, active_clients, became_active = await room_registry.mark_client_active(
                room_id=room_id,
                client_id=joined_client_id,
            )
            await _broadcast_active_if_needed(room_id, active_client, active_clients, became_active)

            clients, typing_update = await room_registry.set_typing(
                room_id=room_id,
                client_id=joined_client_id,
                is_typing=False,
            )
            await _broadcast_typing_if_changed(room_id, clients, typing_update)

            try:
                message = await room_storage.save_message(
                    room_id=room_id,
                    client_id=client.client_id,
                    display_name=client.display_name,
                    text=text,
                )
            except ValueError:
                await _send_error(websocket, room_id, "ROOM_NOT_FOUND", "Room does not exist.")
                continue

            current_clients = await room_registry.list_clients(room_id)
            await _broadcast(current_clients, _chat_message_event(room_id, message))
    except WebSocketDisconnect:
        pass
    finally:
        if joined_client_id is not None:
            left_client, remaining_clients, typing_update = await room_registry.remove_client(
                room_id=room_id,
                client_id=joined_client_id,
            )
            if left_client is not None:
                await _broadcast(remaining_clients, _user_left_event(room_id, left_client))
                await _broadcast_typing_if_changed(room_id, remaining_clients, typing_update)
