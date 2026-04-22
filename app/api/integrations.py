"""
Social-platform OAuth + data endpoints.

Routes:
  GET  /                          → list all connections
  GET  /facebook/authorize        → redirect URL for Facebook OAuth
  GET  /facebook/callback         → handles redirect from Facebook (no JWT)
  DELETE /facebook                → disconnect Facebook
  GET  /facebook/data             → fetch posts & stats from Facebook
  GET  /instagram/authorize       → redirect URL for Instagram OAuth
  GET  /instagram/callback        → handles redirect from Instagram (no JWT)
  DELETE /instagram               → disconnect Instagram
  GET  /instagram/data            → fetch media & stats from Instagram
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.deps import get_current_user
from app.models.user import User
from app.schemas.integration import (
    AuthorizeResponse,
    ConnectionOut,
    DisconnectResponse,
    PlatformDataResponse,
)
from app.services import meta, token_store

router = APIRouter()


def _ensure_meta_configured() -> None:
    settings = get_settings()
    if settings.META_APP_ID and settings.META_APP_SECRET:
        return
    raise HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Meta login is not configured yet. Set META_APP_ID and META_APP_SECRET.",
    )


def _callback_url(request: Request, platform: str) -> str:
    """
    OAuth redirect_uri must match Meta app settings exactly.

    Meta allows http://localhost (not 127.0.0.1) for development. For any other
    host or production, use HTTPS. Set PUBLIC_API_BASE_URL to your public HTTPS
    API URL (e.g. ngrok) when Meta shows the insecure-connection error.
    """
    settings = get_settings()
    explicit = settings.PUBLIC_API_BASE_URL.strip()
    if explicit:
        base = explicit.rstrip("/")
    else:
        base = str(request.base_url).rstrip("/")
        # Meta treats 127.0.0.1 differently from localhost for secure-login rules
        if "127.0.0.1" in base:
            base = base.replace("127.0.0.1", "localhost")
    return f"{base}/api/integrations/{platform}/callback"


# ── List all ──────────────────────────────────────────────────────

@router.get("/", response_model=list[ConnectionOut])
async def list_connections(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = await token_store.list_connections(db, user_id=user.id)
    return rows


# ── Facebook ──────────────────────────────────────────────────────

@router.get("/facebook/authorize", response_model=AuthorizeResponse)
async def fb_authorize(request: Request, user: User = Depends(get_current_user)):
    _ensure_meta_configured()
    settings = get_settings()
    url = meta.build_fb_authorize_url(
        user_id=user.id,
        settings=settings,
        callback_url=_callback_url(request, "facebook"),
    )
    return AuthorizeResponse(url=url)


@router.get("/facebook/callback")
async def fb_callback(
    request: Request,
    code: Annotated[str, Query()],
    state: Annotated[str, Query()],
    db: AsyncSession = Depends(get_db),
):
    _ensure_meta_configured()
    settings = get_settings()
    frontend = settings.FRONTEND_URL.rstrip("/")

    try:
        user_id = meta.verify_state(state, settings.META_STATE_SECRET)
    except ValueError as exc:
        return RedirectResponse(f"{frontend}/integrations?error={exc}")

    try:
        access_token, expires_in = await meta.exchange_fb_code(
            code, _callback_url(request, "facebook"), settings
        )
        profile = await meta.fetch_fb_profile(access_token)
    except Exception:
        return RedirectResponse(f"{frontend}/integrations?error=token_exchange_failed")

    picture_url = None
    pic_data = profile.get("picture", {})
    if isinstance(pic_data, dict):
        picture_url = pic_data.get("data", {}).get("url")

    await token_store.upsert_connection(
        db,
        user_id=user_id,
        platform="facebook",
        platform_user_id=profile["id"],
        access_token=access_token,
        expires_in=expires_in,
        scopes=meta.FB_SCOPES.split(","),
        platform_username=None,
        platform_name=profile.get("name"),
        avatar_url=picture_url,
        raw_profile=profile,
    )
    return RedirectResponse(f"{frontend}/integrations?connected=facebook")


@router.delete("/facebook", response_model=DisconnectResponse)
async def fb_disconnect(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    deleted = await token_store.delete_connection(db, user_id=user.id, platform="facebook")
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Facebook not connected")
    return DisconnectResponse(success=True)


@router.get("/facebook/data", response_model=PlatformDataResponse)
async def fb_data(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conn = await token_store.get_connection(db, user_id=user.id, platform="facebook")
    if not conn:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Facebook not connected")

    posts = await meta.fetch_fb_posts(conn.access_token, limit=50)

    total_likes = 0
    total_comments = 0
    for p in posts:
        total_likes += p.get("likes", {}).get("summary", {}).get("total_count", 0)
        total_comments += p.get("comments", {}).get("summary", {}).get("total_count", 0)

    conn.last_synced_at = datetime.now(timezone.utc)
    await db.commit()

    return PlatformDataResponse(
        platform="facebook",
        posts=posts,
        stats={
            "total_posts": len(posts),
            "total_likes": total_likes,
            "total_comments": total_comments,
            "name": conn.platform_name,
            "avatar_url": conn.avatar_url,
        },
    )


# ── Instagram ─────────────────────────────────────────────────────

@router.get("/instagram/authorize", response_model=AuthorizeResponse)
async def ig_authorize(request: Request, user: User = Depends(get_current_user)):
    _ensure_meta_configured()
    settings = get_settings()
    url = meta.build_ig_authorize_url(
        user_id=user.id,
        settings=settings,
        callback_url=_callback_url(request, "instagram"),
    )
    return AuthorizeResponse(url=url)


@router.get("/instagram/callback")
async def ig_callback(
    request: Request,
    code: Annotated[str, Query()],
    state: Annotated[str, Query()],
    db: AsyncSession = Depends(get_db),
):
    _ensure_meta_configured()
    settings = get_settings()
    frontend = settings.FRONTEND_URL.rstrip("/")

    try:
        user_id = meta.verify_state(state, settings.META_STATE_SECRET)
    except ValueError as exc:
        return RedirectResponse(f"{frontend}/integrations?error={exc}")

    try:
        access_token, expires_in = await meta.exchange_ig_code(
            code, _callback_url(request, "instagram"), settings
        )
        profile = await meta.fetch_ig_profile(access_token)
    except Exception:
        return RedirectResponse(f"{frontend}/integrations?error=token_exchange_failed")

    await token_store.upsert_connection(
        db,
        user_id=user_id,
        platform="instagram",
        platform_user_id=profile["id"],
        access_token=access_token,
        expires_in=expires_in,
        scopes=meta.IG_SCOPES.split(","),
        platform_username=profile.get("username"),
        platform_name=profile.get("username"),
        avatar_url=None,
        raw_profile=profile,
    )
    return RedirectResponse(f"{frontend}/integrations?connected=instagram")


@router.delete("/instagram", response_model=DisconnectResponse)
async def ig_disconnect(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    deleted = await token_store.delete_connection(db, user_id=user.id, platform="instagram")
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Instagram not connected")
    return DisconnectResponse(success=True)


@router.get("/instagram/data", response_model=PlatformDataResponse)
async def ig_data(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conn = await token_store.get_connection(db, user_id=user.id, platform="instagram")
    if not conn:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Instagram not connected")

    media = await meta.fetch_ig_media(conn.access_token, limit=50)

    media_types: dict[str, int] = {}
    for item in media:
        mt = item.get("media_type", "UNKNOWN")
        media_types[mt] = media_types.get(mt, 0) + 1

    conn.last_synced_at = datetime.now(timezone.utc)
    await db.commit()

    return PlatformDataResponse(
        platform="instagram",
        posts=media,
        stats={
            "total_media": len(media),
            "media_types": media_types,
            "username": conn.platform_username,
            "media_count_profile": conn.raw_profile.get("media_count") if conn.raw_profile else None,
        },
    )
