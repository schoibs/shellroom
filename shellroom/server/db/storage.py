"""SQLAlchemy ORM persistence for ShellRoom room metadata."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import delete, desc, func, select

from shellroom.server.db.models import MessageRecord, RoomEventRecord, RoomRecord
from shellroom.server.db.session import Base, SQLiteDatabase
from shellroom.server.model import Message, StoredRoom


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

    async def delete_room(self, room_id: str) -> bool:
        async with self.database.session() as session:
            room_record = await session.get(RoomRecord, room_id)
            if room_record is None:
                return False

            await session.execute(delete(MessageRecord).where(MessageRecord.room_id == room_id))
            await session.execute(delete(RoomEventRecord).where(RoomEventRecord.room_id == room_id))
            await session.execute(delete(RoomRecord).where(RoomRecord.id == room_id))
            await session.commit()

        return True

    async def save_message(
        self,
        room_id: str,
        client_id: str,
        display_name: str,
        text: str,
    ) -> Message:
        now = datetime.now(UTC)
        message_id = f"msg_{uuid4().hex}"

        async with self.database.session() as session:
            room_record = await session.get(RoomRecord, room_id)
            if room_record is None:
                raise ValueError("Room does not exist.")

            room_record.last_active_at = now
            record = MessageRecord(
                id=message_id,
                room_id=room_id,
                client_id=client_id,
                display_name=display_name,
                text=text,
                created_at=now,
            )
            session.add(record)
            await session.commit()

        return Message(
            id=message_id,
            room_id=room_id,
            client_id=client_id,
            display_name=display_name,
            text=text,
            created_at=now,
        )

    async def list_recent_messages(self, room_id: str, limit: int) -> list[Message]:
        async with self.database.session() as session:
            result = await session.execute(
                select(MessageRecord)
                .where(MessageRecord.room_id == room_id)
                .order_by(desc(MessageRecord.created_at))
                .limit(limit)
            )
            records = list(result.scalars().all())

        return [
            Message(
                id=record.id,
                room_id=record.room_id,
                client_id=record.client_id,
                display_name=record.display_name,
                text=record.text,
                created_at=record.created_at,
            )
            for record in reversed(records)
        ]
