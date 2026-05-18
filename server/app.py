"""FastAPI application for the ShellRoom server."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Body, FastAPI, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from server.config import ServerConfig
from server.db.session import SQLiteDatabase
from server.db.storage import RoomStorage
from server.registry import RoomIdGenerationFailed, RoomLimitExceeded, RoomRegistry


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
