"""Async engine/session management."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from relayagents.core.config import Settings, get_settings
from relayagents.core.models import Base


class Database:
    def __init__(self, url: str, *, echo: bool = False) -> None:
        self.url = url
        self.engine: AsyncEngine = create_async_engine(
            url, echo=echo, pool_pre_ping=not url.startswith("sqlite")
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as s:
            yield s

    async def create_all(self) -> None:
        """Create tables directly (tests and SQLite dev). Production uses Alembic."""
        async with self.engine.begin() as conn:
            if self.url.startswith("postgresql"):
                from sqlalchemy import text

                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self.engine.dispose()


_db: Database | None = None


def get_db(settings: Settings | None = None) -> Database:
    global _db
    if _db is None:
        _db = Database((settings or get_settings()).database_url)
    return _db


def set_db(db: Database | None) -> None:
    global _db
    _db = db
