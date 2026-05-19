"""Async client SDK for the ShellRoom HTTP and WebSocket protocol."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
import websockets

DEFAULT_SERVER_URL = "http://localhost:8000"


class ShellRoomError(Exception):
    """Base error for SDK failures."""


class ShellRoomConnectionError(ShellRoomError):
    """Raised when the SDK cannot connect to the server."""


class ShellRoomProtocolError(ShellRoomError):
    """Raised when the server sends an invalid protocol event."""


@dataclass(frozen=True, slots=True)
class CreateRoomResult:
    room_id: str
    invite_url: str
    join_command: str
    websocket_url: str


@dataclass(slots=True)
class RoomSession:
    """A joined ShellRoom WebSocket session."""

    room_id: str
    client_id: str
    display_name: str
    websocket: Any
    closed: bool = False # for idempotency, calling close twice would be fine

    async def send_chat(self, text: str) -> None:
        await self._send_room_event("chat_message", {"text": text})

    async def send_typing_start(self) -> None:
        await self._send_room_event("typing_start", {})

    async def send_typing_stop(self) -> None:
        await self._send_room_event("typing_stop", {})

    async def _send_room_event(self, event_type: str, payload: dict[str, Any]) -> None:
        await self._send(
            {
                "type": event_type,
                "room_id": self.room_id,
                "client_id": self.client_id,
                "payload": payload,
            }
        )

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            raw_event = await self.websocket.recv()
            try:
                event = json.loads(raw_event)
            except json.JSONDecodeError as exc:
                raise ShellRoomProtocolError("Server sent invalid JSON.") from exc

            if not isinstance(event, dict):
                raise ShellRoomProtocolError("Server sent a non-object event.")

            yield event

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self.websocket.close()

    async def _send(self, event: dict[str, Any]) -> None:
        await self.websocket.send(json.dumps(event))


class ShellRoomClient:
    """Small async SDK for creating and joining ShellRoom rooms."""

    def __init__(self, server_url: str = DEFAULT_SERVER_URL) -> None:
        self.server_url = normalize_server_url(server_url)

    async def create_room(self, display_name: str | None = None) -> CreateRoomResult:
        payload: dict[str, str] = {}
        if display_name is not None:
            payload["display_name"] = display_name

        try:
            async with httpx.AsyncClient(base_url=self.server_url, timeout=10.0) as client:
                response = await client.post("/rooms", json=payload)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = _response_detail(exc.response)
            raise ShellRoomConnectionError(f"Room creation failed: {detail}") from exc
        except httpx.HTTPError as exc:
            raise ShellRoomConnectionError(f"Could not reach ShellRoom server: {exc}") from exc

        data = response.json()
        return CreateRoomResult(
            room_id=str(data["room_id"]),
            invite_url=str(data["invite_url"]),
            join_command=str(data["join_command"]),
            websocket_url=str(data["websocket_url"]),
        )

    async def connect(
        self,
        room_id: str,
        display_name: str,
        websocket_url: str | None = None,
    ) -> RoomSession:
        client_id = f"client_{uuid4().hex}"
        url = websocket_url or websocket_url_for_room(self.server_url, room_id)

        try:
            websocket = await websockets.connect(url)
        except OSError as exc:
            raise ShellRoomConnectionError(f"Could not connect to room WebSocket: {exc}") from exc

        session = RoomSession(
            room_id=room_id,
            client_id=client_id,
            display_name=display_name,
            websocket=websocket,
        )

        await session._send(
            {
                "type": "join_room",
                "room_id": room_id,
                "client_id": client_id,
                "payload": {"display_name": display_name},
            }
        )
        return session


def normalize_server_url(server_url: str) -> str:
    value = server_url.strip()
    if not value:
        return DEFAULT_SERVER_URL
    if "://" not in value:
        value = f"http://{value}"
    return value.rstrip("/")


def websocket_url_for_room(server_url: str, room_id: str) -> str:
    normalized = normalize_server_url(server_url)
    parts = urlsplit(normalized)
    scheme = "wss" if parts.scheme == "https" else "ws"
    path = parts.path.rstrip("/") + f"/ws/{room_id}"
    return urlunsplit((scheme, parts.netloc, path, "", ""))


def _response_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text or response.reason_phrase

    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, str):
        return detail
    return response.reason_phrase
