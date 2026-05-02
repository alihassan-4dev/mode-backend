"""SQLAlchemy engines and startup schema migration helpers."""

import asyncio
import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.inspection import inspect
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings

logger = logging.getLogger(__name__)
BACKEND_ROOT = Path(__file__).resolve().parents[2]


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


def _build_alembic_config(database_url: str) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _infer_unversioned_revision(sync_engine: Engine) -> str | None:
    inspector = inspect(sync_engine)
    tables = set(inspector.get_table_names())
    known_tables = tables - {"alembic_version"}
    if not known_tables:
        return None
    if "post_reports" in known_tables:
        post_report_columns = {
            column["name"] for column in inspector.get_columns("post_reports")
        }
        mode_columns = {"mode_label", "mode_confidence", "mode_drivers"}
        return "004" if mode_columns.issubset(post_report_columns) else "003"
    if {"chat_sessions", "chat_messages"}.issubset(known_tables):
        return "002"
    if {"users", "social_connections"}.issubset(known_tables):
        return "001"
    raise RuntimeError(
        "Found existing database tables without Alembic version metadata, "
        f"but could not infer their revision safely: {sorted(known_tables)}"
    )


def _migrate_database_to_head() -> None:
    settings = get_settings()
    url = settings.database_url_sync
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not configured. Set it in backend/.env to enable database features."
        )

    sync_engine = _get_sync_engine()
    if sync_engine is None:
        raise RuntimeError(
            "DATABASE_URL is not configured. Set it in backend/.env to enable database features."
        )

    try:
        config = _build_alembic_config(url)
        inspector = inspect(sync_engine)
        tables = set(inspector.get_table_names())
        if "alembic_version" not in tables and tables:
            inferred_revision = _infer_unversioned_revision(sync_engine)
            if inferred_revision is not None:
                stamp_target = "head" if inferred_revision == "004" else inferred_revision
                logger.warning(
                    "Database has tables but no alembic_version table; "
                    "stamping revision %s before upgrade.",
                    stamp_target,
                )
                command.stamp(config, stamp_target)
        command.upgrade(config, "head")
    finally:
        sync_engine.dispose()


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
    """Apply required schema migrations when the app starts."""
    await asyncio.to_thread(_migrate_database_to_head)
