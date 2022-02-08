"""Database engine/session lifecycle for FastAPI."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import Settings
from .models import Base


def _prepare_sqlite_path(database_url: str) -> None:
    if not database_url.startswith("sqlite"):
        return

    marker = "///"
    if marker not in database_url:
        return

    sqlite_path = database_url.split(marker, maxsplit=1)[1]
    if sqlite_path.startswith(":"):
        return

    path = Path(sqlite_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    path.parent.mkdir(parents=True, exist_ok=True)


class Database:
    """Holds engine and async session factory."""

    def __init__(self, settings: Settings):
        _prepare_sqlite_path(settings.database_url)
        self.engine: AsyncEngine = create_async_engine(
            settings.database_url,
            future=True,
            pool_pre_ping=True,
        )
        self.session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    async def init_models(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()
