"""
Run the API:

    uv run uvicorn main:app --reload --host 0.0.0.0 --port 8000

Or:

    python -m uvicorn main:app --reload --host 0.0.0.0 --port 8000
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import app.models  # noqa: F401
from app.api.auth import router as auth_router
from app.api.chat import router as chat_router
from app.api.routes import router as api_router
from app.api.integrations import router as integrations_router
from app.api.social_posts import router as social_posts_router
from app.core.config import get_settings
from app.core.database import initialize_database

settings = get_settings()
_log = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Log DB mode and SQLite 3 engine version for local file DB."""
    await initialize_database()
    if settings.USE_LOCAL_SQLITE:
        _log.info("Database: local SQLite 3 file at ./db/app.db (USE_LOCAL_SQLITE=true; DATABASE_URL is ignored).")
    if settings.database_url_async.startswith("sqlite"):
        import sqlite3

        _log.info(
            "SQLite 3 library version: sqlite3.sqlite_version=%s",
            sqlite3.sqlite_version,
        )
    _log.info("Chat history mode: %s", settings.CHAT_HISTORY_MODE.strip().lower() or "session")
    yield


app = FastAPI(title="E-Mode API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix="/api")
app.include_router(auth_router, prefix="/api")
app.include_router(chat_router, prefix="/api")
app.include_router(integrations_router, prefix="/api/integrations")
app.include_router(social_posts_router, prefix="/api")


@app.get("/")
def root() -> dict:
    return {"service": "e-mode-api", "docs": "/docs"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=settings.PORT,
        reload=True,
    )
