"""SQLAlchemy ORM persistence for ShellRoom room metadata."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select

from server.db.models import RoomRecord
from server.db.session import Base, SQLiteDatabase
from server.model import StoredRoom


class RoomStorage:
    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    async def initialize(self) -> None:
        async with self.database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def count_active_rooms(self) -> int:
        async with self.database.session() as session:
            result = await session.execute(
                select(func.count())
                .select_from(RoomRecord)
                .where(RoomRecord.status == "active", RoomRecord.closed_at.is_(None))
            )
            return int(result.scalar_one())

    async def create_room(self, room_id: str, max_users: int) -> StoredRoom:
        now = datetime.now(UTC)

        record = RoomRecord(
            id=room_id,
            created_at=now,
            last_active_at=now,
            closed_at=None,
            max_users=max_users,
            status="active",
        )

        async with self.database.session() as session:
            session.add(record)
            await session.commit()

        return StoredRoom(
            id=record.id,
            created_at=record.created_at,
            last_active_at=record.last_active_at,
            closed_at=record.closed_at,
            max_users=record.max_users,
            status=record.status,
        )

    async def get_room(self, room_id: str) -> StoredRoom | None:
        async with self.database.session() as session:
            record = await session.get(RoomRecord, room_id)

        if record is None:
            return None

        return StoredRoom(
            id=record.id,
            created_at=record.created_at,
            last_active_at=record.last_active_at,
            closed_at=record.closed_at,
            max_users=record.max_users,
            status=record.status,
        )
