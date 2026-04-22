"""SQLAlchemy engines. Local dev uses SQLite 3 via `sqlite` / `sqlite+aiosqlite` URLs."""

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


@event.listens_for(Engine, "connect")
def _sqlite_enable_foreign_keys(dbapi_connection, _connection_record) -> None:
    # Works for sqlite DB-API connections; harmless no-op for others.
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    except Exception:
        pass
    finally:
        cursor.close()


def _get_async_engine():
    settings = get_settings()
    url = settings.database_url_async
    if not url:
        return None
    if url.startswith("sqlite"):
        Path("db").mkdir(exist_ok=True)
        return create_async_engine(url, echo=False, connect_args={"check_same_thread": False})
    return create_async_engine(url, echo=False, pool_pre_ping=True)


def _get_sync_engine():
    settings = get_settings()
    url = settings.database_url_sync
    if not url:
        return None
    if url.startswith("sqlite"):
        Path("db").mkdir(exist_ok=True)
        return create_engine(url, echo=False, connect_args={"check_same_thread": False})
    return create_engine(url, echo=False, pool_pre_ping=True)


_async_engine = None
_async_session_factory = None


def get_async_engine():
    global _async_engine
    if _async_engine is None:
        _async_engine = _get_async_engine()
    return _async_engine


def get_async_session_factory():
    global _async_session_factory
    if _async_session_factory is None:
        engine = get_async_engine()
        if engine is None:
            return None
        _async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return _async_session_factory


async def get_db() -> AsyncSession:
    """FastAPI dependency that yields a DB session."""
    factory = get_async_session_factory()
    if factory is None:
        raise RuntimeError(
            "DATABASE_URL is not configured. Set it in backend/.env to enable database features."
        )
    async with factory() as session:
        yield session


async def initialize_database() -> None:
    """Create required tables when the app starts."""
    engine = get_async_engine()
    if engine is None:
        raise RuntimeError(
            "DATABASE_URL is not configured. Set it in backend/.env to enable database features."
        )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
