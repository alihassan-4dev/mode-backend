"""
CRUD operations on social_connections via SQLAlchemy async sessions.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.social_connection import SocialConnection


async def upsert_connection(
    db: AsyncSession,
    *,
    user_id: str,
    platform: str,
    platform_user_id: str,
    access_token: str,
    expires_in: int | None = None,
    scopes: list[str] | None = None,
    platform_username: str | None = None,
    platform_name: str | None = None,
    avatar_url: str | None = None,
    raw_profile: dict | None = None,
) -> SocialConnection:
    stmt = select(SocialConnection).where(
        SocialConnection.user_id == user_id,
        SocialConnection.platform == platform,
    )
    result = await db.execute(stmt)
    conn = result.scalar_one_or_none()

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=expires_in) if expires_in else None

    if conn:
        conn.platform_user_id = platform_user_id
        conn.access_token = access_token
        conn.token_expires_at = expires_at
        conn.scopes = scopes
        conn.platform_username = platform_username
        conn.platform_name = platform_name
        conn.avatar_url = avatar_url
        conn.raw_profile = raw_profile or {}
        conn.last_synced_at = now
    else:
        conn = SocialConnection(
            user_id=user_id,
            platform=platform,
            platform_user_id=platform_user_id,
            access_token=access_token,
            token_expires_at=expires_at,
            scopes=scopes,
            platform_username=platform_username,
            platform_name=platform_name,
            avatar_url=avatar_url,
            raw_profile=raw_profile or {},
            connected_at=now,
            last_synced_at=now,
        )
        db.add(conn)

    await db.commit()
    await db.refresh(conn)
    return conn


async def get_connection(db: AsyncSession, *, user_id: str, platform: str) -> SocialConnection | None:
    stmt = select(SocialConnection).where(
        SocialConnection.user_id == user_id,
        SocialConnection.platform == platform,
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def list_connections(db: AsyncSession, *, user_id: str) -> list[SocialConnection]:
    stmt = select(SocialConnection).where(SocialConnection.user_id == user_id).order_by(SocialConnection.platform)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def delete_connection(db: AsyncSession, *, user_id: str, platform: str) -> bool:
    stmt = delete(SocialConnection).where(
        SocialConnection.user_id == user_id,
        SocialConnection.platform == platform,
    )
    result = await db.execute(stmt)
    await db.commit()
    return result.rowcount > 0
