from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.deps import get_current_user
from app.models.user import User
from app.schemas.social_posts import SocialPostOut, SocialPostsMeta, SocialPostsResponse
from app.services import meta, post_ai, token_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/social-posts", tags=["social-posts"])

PlatformType = Literal["facebook", "instagram"]
MAX_LIMIT = 20


def _normalize_facebook_post(post: dict) -> SocialPostOut:
    likes = post.get("likes", {}).get("summary", {}).get("total_count", 0)
    comments = post.get("comments", {}).get("summary", {}).get("total_count", 0)
    permalink = post.get("permalink_url")
    if not permalink and post.get("id"):
        permalink = f"https://www.facebook.com/{post['id']}"
    return SocialPostOut(
        platform="facebook",
        post_id=str(post.get("id", "")),
        text=post.get("message"),
        created_at=post.get("created_time"),
        permalink=permalink,
        media_type=post.get("type"),
        media_url=None,
        likes_count=int(likes or 0),
        comments_count=int(comments or 0),
    )


def _normalize_instagram_media(post: dict) -> SocialPostOut:
    return SocialPostOut(
        platform="instagram",
        post_id=str(post.get("id", "")),
        text=post.get("caption"),
        created_at=post.get("timestamp"),
        permalink=post.get("permalink"),
        media_type=post.get("media_type"),
        media_url=post.get("media_url") or post.get("thumbnail_url"),
        likes_count=None,
        comments_count=None,
    )


@router.get("", response_model=SocialPostsResponse)
async def social_posts(
    platform: Annotated[PlatformType, Query()],
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 10,
    cursor: Annotated[str | None, Query()] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Cursor reserved for next MVP iteration.
    _ = cursor
    settings = get_settings()
    conn = await token_store.get_connection(db, user_id=user.id, platform=platform)
    if not conn:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{platform.capitalize()} not connected",
        )

    notice: str | None = None
    try:
        if platform == "facebook":
            fb_result = await meta.fetch_fb_posts_detailed(conn.access_token, limit=limit)
            raw_posts = fb_result.posts
            if fb_result.tried_npe_fallback and not raw_posts:
                notice = (
                    "Facebook returned no user posts for this account type (New Pages experience). "
                    "Your current user-post permissions are working, but Meta does not expose posts on this endpoint."
                )
            posts = [_normalize_facebook_post(item) for item in raw_posts]
        else:
            raw_posts = await meta.fetch_ig_media(conn.access_token, limit=limit)
            posts = [_normalize_instagram_media(item) for item in raw_posts]
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code if exc.response is not None else 502
        err_subcode = None
        if exc.response is not None:
            try:
                err_subcode = exc.response.json().get("error", {}).get("error_subcode")
            except ValueError:
                err_subcode = None
        logger.warning(
            "Social posts fetch failed user_id=%s platform=%s status=%s subcode=%s",
            user.id,
            platform,
            status_code,
            err_subcode,
        )
        if status_code in {400, 401, 403}:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"{platform.capitalize()} token/permissions are invalid or expired. Reconnect {platform.capitalize()}.",
            ) from exc
        else:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"{platform.capitalize()} rejected the request. Try again in a moment.",
            ) from exc

    posts = [post for post in posts if post.post_id]
    posts = await post_ai.attach_basic_ai_analysis(posts=posts, settings=settings)

    conn.last_synced_at = datetime.now(timezone.utc)
    await db.commit()

    return SocialPostsResponse(
        posts=posts,
        meta=SocialPostsMeta(
            platform=platform,
            count=len(posts),
            limit=limit,
            next_cursor=None,
            fetched_at=datetime.now(timezone.utc),
            notice=notice,
        ),
    )
