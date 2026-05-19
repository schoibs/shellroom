"""SQLAlchemy database lifecycle for the ShellRoom server."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class for SQLAlchemy ORM models."""


def sqlite_path_from_url(database_url: str) -> str:
    """
    If `sqlite:///./shellroom.db`, returns './shellroom.db'
    """
    if database_url in {"sqlite://", ":memory:"} or database_url.startswith("sqlite:///:memory:"):
        raise ValueError(
            "SHELLROOM_DATABASE_URL must use a file-backed sqlite:/// path; "
            "in-memory SQLite databases are not supported"
        )

    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise ValueError("SHELLROOM_DATABASE_URL must start with sqlite:///")

    return database_url.removeprefix(prefix)


def async_sqlite_url_from_url(database_url: str) -> str:
    """
    Converts a normal SQLite URL into an async SQLAlchemy URL
    """
    return "sqlite+aiosqlite:///" + sqlite_path_from_url(database_url)


class SQLiteDatabase:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.path = sqlite_path_from_url(database_url)
        self.url = async_sqlite_url_from_url(database_url)
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            raise RuntimeError("SQLite database has not been connected.")
        return self._engine

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is None:
            raise RuntimeError("SQLite database has not been connected.")
        return self._session_factory

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            yield session

    def connect(self) -> None:
        """
        Creates the engine and session factory.
        """
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)

        self._engine = create_async_engine(self.url)
        self._session_factory = async_sessionmaker(
            self._engine,
            expire_on_commit=False,
        )

        @event.listens_for(self._engine.sync_engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            # SQLite does not always enforce foreign keys by default. This enables checks like: a message cannot point to a room that does not exist.
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None
