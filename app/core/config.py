from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parents[2]
LOCAL_SQLITE_PATH = BACKEND_ROOT / "db" / "app.db"
# Default local SQLite 3 file (no configuration required when USE_LOCAL_SQLITE=true).
LOCAL_SQLITE_ASYNC_URL = f"sqlite+aiosqlite:///{LOCAL_SQLITE_PATH.as_posix()}"
LOCAL_SQLITE_SYNC_URL = f"sqlite:///{LOCAL_SQLITE_PATH.as_posix()}"


class Settings(BaseSettings):
    """Loaded from environment / `.env` in the backend root."""

    # When True (default), the API and Alembic always use LOCAL_SQLITE_* paths below.
    # Set False only if you intentionally use a remote DATABASE_URL (Postgres, etc.).
    USE_LOCAL_SQLITE: bool = True

    # Optional remote DSN — only used when USE_LOCAL_SQLITE is False.
    DATABASE_URL: str = ""
    JWT_SECRET_KEY: str = "change-me-to-a-long-random-secret"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRES_MINUTES: int = 60 * 24 * 7  # 7 days
    PORT: int = 8000
    CORS_ORIGIN: str = (
        "http://localhost:5173,http://127.0.0.1:5173,"
        "http://localhost:8080,http://127.0.0.1:8080,"
        "http://localhost:8081,http://127.0.0.1:8081"
    )
    FRONTEND_URL: str = "http://localhost:8080"
    # If set, OAuth redirect_uri uses this base (HTTPS tunnel URL in dev). Meta rejects plain http:// except localhost.
    PUBLIC_API_BASE_URL: str = ""
    # Optional explicit redirect URI overrides (recommended for production stability).
    FACEBOOK_REDIRECT_URI: str = ""
    INSTAGRAM_REDIRECT_URI: str = ""

    META_APP_ID: str = ""
    META_APP_SECRET: str = ""
    FB_APP_ID: str = ""
    FB_APP_SECRET: str = ""
    IG_APP_ID: str = ""
    IG_APP_SECRET: str = ""
    META_STATE_SECRET: str = "change-me-to-a-random-32-char-string"
    # Optional comma-separated Facebook Login scopes (e.g. pages_show_list,pages_read_engagement).
    # Only set after your Meta app is allowed to request those permissions; otherwise OAuth shows
    # "Invalid Scopes". Default is empty so Connect works for basic profile + user_posts only.
    FB_EXTRA_LOGIN_SCOPES: str = ""
    GROQ_API_KEY: str = ""
    GROQ_BASE_URL: str = "https://api.groq.com/openai/v1"
    GROQ_MODEL: str = "openai/gpt-oss-120b"
    AGENT_GROQ_MODEL: str = "llama-3.3-70b-versatile"
    AGENT_SYSTEM_NAME: str = "Counseling Corner"
    AGENT_TEMPERATURE: float = 0.2
    AGENT_TIMEOUT_SECONDS: float = 20.0
    CHAT_HISTORY_MODE: str = "persistent"
    # Background report refresh interval (minutes). Reports regenerate via the LLM
    # for each connected user on this cadence.
    REPORTS_REFRESH_MINUTES: int = 5
    # Max posts per platform fetched and analyzed per refresh cycle.
    REPORTS_POSTS_PER_PLATFORM: int = 10
    LOG_LEVEL: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGIN.split(",") if o.strip()]

    @property
    def database_url_sync(self) -> str:
        """Sync URL for Alembic migrations."""
        if self.USE_LOCAL_SQLITE:
            return LOCAL_SQLITE_SYNC_URL
        url = (self.DATABASE_URL or "").strip()
        if not url:
            return LOCAL_SQLITE_SYNC_URL
        if url.startswith("sqlite+aiosqlite://"):
            return url.replace("sqlite+aiosqlite://", "sqlite://", 1)
        if url.startswith("postgresql+asyncpg://"):
            return url.replace("postgresql+asyncpg://", "postgresql://", 1)
        return url

    @property
    def database_url_async(self) -> str:
        """Async URL for SQLAlchemy engine."""
        if self.USE_LOCAL_SQLITE:
            return LOCAL_SQLITE_ASYNC_URL
        url = (self.DATABASE_URL or "").strip()
        if not url:
            return LOCAL_SQLITE_ASYNC_URL
        if url.startswith("sqlite://"):
            return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
        if url.startswith("sqlite+aiosqlite://"):
            return url
        if url.startswith("postgresql://") and "+asyncpg" not in url.split("://", 1)[0]:
            return url.replace("postgresql://", "postgresql+asyncpg://", 1)
        if url.startswith("postgres://"):
            return url.replace("postgres://", "postgresql+asyncpg://", 1)
        return url

    @property
    def uses_persistent_chat_history(self) -> bool:
        return self.CHAT_HISTORY_MODE.strip().lower() == "persistent"


@lru_cache
def get_settings() -> Settings:
    return Settings()
