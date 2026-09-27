"""SQLAlchemy async session factory and FastAPI dependency."""

from collections.abc import AsyncGenerator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from audr.config import get_settings


def _make_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    return create_async_engine(database_url, echo=echo)


def _make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@lru_cache
def _get_session_factory() -> async_sessionmaker[AsyncSession]:
    settings = get_settings()
    engine = _make_engine(settings.database_url, echo=settings.debug)
    return _make_session_factory(engine)


async def get_db() -> AsyncGenerator[AsyncSession]:
    async with _get_session_factory()() as session:
        yield session
