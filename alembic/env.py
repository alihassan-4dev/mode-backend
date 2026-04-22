"""
Alembic migration environment.

Reads resolved DB URL from app settings (backend/.env). Default is local SQLite 3 (./db/app.db).

Usage:
    cd backend
    alembic revision --autogenerate -m "describe change"
    alembic upgrade head
"""

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

from app.core.config import get_settings
from app.core.database import Base

# Import all models so Base.metadata picks them up for autogenerate
import app.models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Override sqlalchemy.url from app settings
settings = get_settings()
db_url = settings.database_url_sync
if not db_url:
    raise RuntimeError(
        "Resolved database URL is empty. Check backend/.env (default: local SQLite 3 at ./db/app.db), "
        "then run: cd backend && python -m alembic upgrade head"
    )
config.set_main_option("sqlalchemy.url", db_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    if not url or url.startswith("driver://"):
        raise RuntimeError("DATABASE_URL missing — set it in backend/.env before running Alembic.")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
