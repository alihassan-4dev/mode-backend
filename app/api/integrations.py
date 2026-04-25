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
import logging
import time
from typing import Annotated

import httpx
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
logger = logging.getLogger(__name__)
IG_PENDING_OAUTH_TTL_SECONDS = 15 * 60
_ig_pending_oauth_by_ip: dict[str, tuple[str, float, str]] = {}


def _ensure_meta_configured(platform: str) -> None:
    settings = get_settings()
    try:
        meta.get_platform_app_credentials(platform, settings)
        return
    except ValueError as exc:
        detail = str(exc)
    raise HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=detail,
    )


def _callback_url(request: Request, platform: str) -> str:
    """
    OAuth redirect_uri must match Meta app settings exactly.

    Meta allows http://localhost (not 127.0.0.1) for development. For any other
    host or production, use HTTPS. Set PUBLIC_API_BASE_URL to your public HTTPS
    API URL (e.g. ngrok) when Meta shows the insecure-connection error.
    """
    settings = get_settings()
    if platform == "facebook" and settings.FACEBOOK_REDIRECT_URI.strip():
        return settings.FACEBOOK_REDIRECT_URI.strip()
    if platform == "instagram" and settings.INSTAGRAM_REDIRECT_URI.strip():
        return settings.INSTAGRAM_REDIRECT_URI.strip()

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
    _ensure_meta_configured("facebook")
    settings = get_settings()
    _, _, cred_source = meta.get_platform_app_credentials("facebook", settings)
    callback = _callback_url(request, "facebook")
    logger.info(
        "Facebook OAuth authorize requested user_id=%s callback=%s cred_source=%s",
        user.id,
        callback,
        cred_source,
    )
    url = meta.build_fb_authorize_url(
        user_id=user.id,
        settings=settings,
        callback_url=callback,
    )
    return AuthorizeResponse(url=url)


@router.get("/facebook/callback")
async def fb_callback(
    request: Request,
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
    error_reason: Annotated[str | None, Query()] = None,
    error_description: Annotated[str | None, Query()] = None,
    db: AsyncSession = Depends(get_db),
):
    _ensure_meta_configured("facebook")
    settings = get_settings()
    frontend = settings.FRONTEND_URL.rstrip("/")
    callback = _callback_url(request, "facebook")

    if error:
        logger.warning(
            "Facebook OAuth returned provider error error=%s reason=%s description=%s callback=%s",
            error,
            error_reason,
            error_description,
            callback,
        )
        return RedirectResponse(f"{frontend}/integrations?error=facebook_oauth_{error}")

    if not code or not state:
        logger.warning("Facebook callback missing required query params callback=%s", callback)
        return RedirectResponse(f"{frontend}/integrations?error=missing_oauth_code_or_state")

    try:
        user_id = meta.verify_state(state, settings.META_STATE_SECRET)
    except ValueError as exc:
        logger.warning("Facebook OAuth state verification failed: %s", exc)
        return RedirectResponse(f"{frontend}/integrations?error={exc}")

    try:
        access_token, expires_in = await meta.exchange_fb_code(
            code, callback, settings
        )
        profile = await meta.fetch_fb_profile(access_token)
    except Exception:
        logger.exception("Facebook token exchange/profile fetch failed callback=%s", callback)
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
    logger.info("Facebook connected user_id=%s fb_profile_id=%s", user_id, profile.get("id"))
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

    posts: list[dict] = []
    try:
        posts = await meta.fetch_fb_posts(conn.access_token, limit=50)
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code if exc.response is not None else 502
        body = exc.response.text if exc.response is not None else ""
        logger.warning(
            "Facebook data sync failed user_id=%s status=%s body=%s",
            user.id,
            status_code,
            body,
        )

        # Some business/page setups return this known Graph API limitation for /me/posts.
        # Treat as a degraded-but-successful sync so UX still updates "last synced".
        err_subcode = None
        if exc.response is not None:
            try:
                err_subcode = (
                    exc.response.json().get("error", {}).get("error_subcode")
                )
            except ValueError:
                err_subcode = None
        if status_code == 400 and err_subcode == 2069030:
            logger.info(
                "Facebook sync completed in limited mode user_id=%s reason=new_pages_experience",
                user.id,
            )
            posts = []
        else:
            detail = "Facebook rejected the sync request. Reconnect Facebook and try again."
            if status_code in {400, 401, 403}:
                detail = "Facebook token/permissions are invalid or expired. Reconnect Facebook."
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail) from exc

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
    _ensure_meta_configured("instagram")
    settings = get_settings()
    _, _, cred_source = meta.get_platform_app_credentials("instagram", settings)
    callback = _callback_url(request, "instagram")
    logger.info(
        "Instagram OAuth authorize requested user_id=%s callback=%s cred_source=%s",
        user.id,
        callback,
        cred_source,
    )
    if request.client and request.client.host:
        _ig_pending_oauth_by_ip[request.client.host] = (user.id, time.time(), callback)
    url = meta.build_ig_authorize_url(
        user_id=user.id,
        settings=settings,
        callback_url=callback,
    )
    logger.info("Instagram OAuth authorize URL generated callback=%s", callback)
    return AuthorizeResponse(url=url)


@router.get("/instagram/callback")
async def ig_callback(
    request: Request,
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
    error_reason: Annotated[str | None, Query()] = None,
    error_description: Annotated[str | None, Query()] = None,
    db: AsyncSession = Depends(get_db),
):
    _ensure_meta_configured("instagram")
    settings = get_settings()
    frontend = settings.FRONTEND_URL.rstrip("/")
    callback = _callback_url(request, "instagram")

    if error:
        logger.warning(
            "Instagram OAuth returned provider error error=%s reason=%s description=%s callback=%s",
            error,
            error_reason,
            error_description,
            callback,
        )
        return RedirectResponse(f"{frontend}/integrations?error=instagram_oauth_{error}")

    if not code:
        logger.warning("Instagram callback missing required query params callback=%s", callback)
        return RedirectResponse(f"{frontend}/integrations?error=missing_oauth_code_or_state")

    user_id: str | None = None
    if state:
        try:
            user_id = meta.verify_state(state, settings.META_STATE_SECRET)
        except ValueError as exc:
            logger.warning("Instagram OAuth state verification failed: %s", exc)
            return RedirectResponse(f"{frontend}/integrations?error={exc}")
    else:
        # Instagram Business Login can omit state on some callback flows.
        # For local development, recover user from the most recent authorize call by IP.
        ip = request.client.host if request.client else ""
        pending = _ig_pending_oauth_by_ip.get(ip)
        if pending:
            pending_user_id, issued_at, pending_callback = pending
            if (time.time() - issued_at) <= IG_PENDING_OAUTH_TTL_SECONDS and pending_callback == callback:
                user_id = pending_user_id
                logger.warning(
                    "Instagram callback missing state; using pending OAuth session fallback ip=%s user_id=%s",
                    ip,
                    user_id,
                )
        if not user_id:
            logger.warning(
                "Instagram callback missing state and no valid pending session callback=%s ip=%s",
                callback,
                ip,
            )
            return RedirectResponse(f"{frontend}/integrations?error=missing_oauth_code_or_state")

    try:
        access_token, expires_in = await meta.exchange_ig_code(
            code, callback, settings
        )
        profile = await meta.fetch_ig_profile(access_token)
    except Exception:
        logger.exception("Instagram token exchange/profile fetch failed callback=%s", callback)
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
    if request.client and request.client.host:
        _ig_pending_oauth_by_ip.pop(request.client.host, None)
    logger.info("Instagram connected user_id=%s ig_profile_id=%s", user_id, profile.get("id"))
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
