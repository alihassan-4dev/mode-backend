import hashlib
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

from app.core.config import get_settings

def hash_password(password: str) -> str:
    """
    Hash passwords with bcrypt, after SHA-256 pre-hash.

    Why pre-hash: bcrypt only accepts first 72 bytes of input. Pre-hashing avoids
    failures/truncation for long unicode passwords while keeping bcrypt as KDF.
    """
    normalized = hashlib.sha256(password.encode("utf-8")).digest()
    return bcrypt.hashpw(normalized, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    normalized = hashlib.sha256(password.encode("utf-8")).digest()
    return bcrypt.checkpw(normalized, password_hash.encode("utf-8"))


def create_access_token(user_id: str) -> str:
    settings = get_settings()
    expires_at = datetime.now(UTC) + timedelta(minutes=settings.JWT_EXPIRES_MINUTES)
    payload = {"sub": user_id, "exp": expires_at, "iat": datetime.now(UTC)}
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    settings = get_settings()
    return jwt.decode(
        token,
        settings.JWT_SECRET_KEY,
        algorithms=[settings.JWT_ALGORITHM],
    )
