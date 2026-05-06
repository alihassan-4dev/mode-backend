"""
Facebook + Instagram OAuth helpers and Graph API client.

Facebook flow:
  1. build_fb_authorize_url  → user opens in browser
  2. exchange_fb_code        → short-lived token → long-lived token
  3. fetch_fb_profile        → /me with fields
  4. fetch_fb_posts          → /me/posts with progressive field fallbacks, then optional:
     - /me?fields=feed.limit(n){...} (nested feed when /me/posts never succeeds; NPE and similar)

Instagram Business Login flow:
  1. build_ig_authorize_url
  2. exchange_ig_code        → short → long-lived token
  3. fetch_ig_profile        → /me
  4. fetch_ig_media          → /me/media
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from urllib.parse import urlencode

from httpx import AsyncClient, HTTPStatusError

from app.core.config import Settings

FB_GRAPH = "https://graph.facebook.com/v21.0"
# OAuthException codes that indicate a bad/expired/revoked token — must propagate, never degrade to empty.
_FB_OAUTH_HARD_FAIL_CODES = frozenset({102, 190, 458})
FB_OAUTH_DIALOG = "https://www.facebook.com/v21.0/dialog/oauth"
# Instagram Business Login authorization window
IG_OAUTH_AUTHORIZE = "https://www.instagram.com/oauth/authorize"
IG_OAUTH_TOKEN = "https://api.instagram.com/oauth/access_token"
IG_GRAPH = "https://graph.instagram.com"

STATE_MAX_AGE = 900  # 15 minutes
logger = logging.getLogger(__name__)


def get_platform_app_credentials(platform: str, settings: Settings) -> tuple[str, str, str]:
    """
    Resolve app credentials for a given platform.

    Preference order:
      - facebook: FB_APP_ID/FB_APP_SECRET, then META_APP_ID/META_APP_SECRET
      - instagram: IG_APP_ID/IG_APP_SECRET, then META_APP_ID/META_APP_SECRET
    """
    p = platform.strip().lower()
    if p == "facebook":
        app_id = (settings.FB_APP_ID or settings.META_APP_ID).strip()
        app_secret = (settings.FB_APP_SECRET or settings.META_APP_SECRET).strip()
        source = "facebook_specific" if settings.FB_APP_ID and settings.FB_APP_SECRET else "meta_fallback"
    elif p == "instagram":
        app_id = (settings.IG_APP_ID or settings.META_APP_ID).strip()
        app_secret = (settings.IG_APP_SECRET or settings.META_APP_SECRET).strip()
        source = "instagram_specific" if settings.IG_APP_ID and settings.IG_APP_SECRET else "meta_fallback"
    else:
        raise ValueError(f"Unsupported platform: {platform}")

    if not app_id or not app_secret:
        raise ValueError(
            f"{platform.capitalize()} login is not configured yet. "
            f"Set {platform.upper()}_APP_ID/{platform.upper()}_APP_SECRET "
            f"or fallback META_APP_ID/META_APP_SECRET."
        )

    return app_id, app_secret, source


# ── State (CSRF) helpers ──────────────────────────────────────────

def create_state(user_id: str, secret: str) -> str:
    ts = str(int(time.time()))
    payload = f"{user_id}:{ts}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def verify_state(state: str, secret: str) -> str:
    """Return user_id or raise ValueError."""
    parts = state.split(":")
    if len(parts) != 3:
        raise ValueError("Invalid state format")
    user_id, ts, sig = parts
    expected = hmac.new(secret.encode(), f"{user_id}:{ts}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        raise ValueError("Invalid state signature")
    if int(time.time()) - int(ts) > STATE_MAX_AGE:
        raise ValueError("State expired")
    return user_id


# ── Facebook ──────────────────────────────────────────────────────

# Default scopes: safe for any app type. Page-related scopes must be added via Settings.FB_EXTRA_LOGIN_SCOPES
# only after they are enabled for your app in the Meta Developer Dashboard (otherwise OAuth returns
# "Invalid Scopes").
FB_BASE_LOGIN_SCOPES = "public_profile,email,user_posts,user_likes"


def facebook_login_scopes(settings: Settings) -> str:
    """Facebook Login `scope` string: base scopes plus optional FB_EXTRA_LOGIN_SCOPES from env."""
    extra = (settings.FB_EXTRA_LOGIN_SCOPES or "").strip()
    if not extra:
        return FB_BASE_LOGIN_SCOPES
    return f"{FB_BASE_LOGIN_SCOPES},{extra}"


def build_fb_authorize_url(user_id: str, settings: Settings, callback_url: str) -> str:
    app_id, _, _ = get_platform_app_credentials("facebook", settings)
    state = create_state(user_id, settings.META_STATE_SECRET)
    params = {
        "client_id": app_id,
        "redirect_uri": callback_url,
        "scope": facebook_login_scopes(settings),
        "response_type": "code",
        "state": state,
    }
    return f"{FB_OAUTH_DIALOG}?{urlencode(params)}"


async def exchange_fb_code(code: str, callback_url: str, settings: Settings) -> tuple[str, int | None]:
    """Exchange auth code → short-lived → long-lived token. Returns (token, expires_in_seconds)."""
    app_id, app_secret, _ = get_platform_app_credentials("facebook", settings)
    async with AsyncClient(timeout=15.0) as client:
        # Short-lived token
        resp = await client.get(
            f"{FB_GRAPH}/oauth/access_token",
            params={
                "client_id": app_id,
                "client_secret": app_secret,
                "redirect_uri": callback_url,
                "code": code,
            },
        )
        try:
            resp.raise_for_status()
        except HTTPStatusError as exc:
            logger.error(
                "Facebook token exchange failed (short-lived): status=%s body=%s",
                exc.response.status_code if exc.response is not None else "unknown",
                exc.response.text if exc.response is not None else "",
            )
            raise
        short_data = resp.json()
        short_token = short_data["access_token"]

        # Long-lived token (≈60 days)
        resp2 = await client.get(
            f"{FB_GRAPH}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "fb_exchange_token": short_token,
            },
        )
        try:
            resp2.raise_for_status()
        except HTTPStatusError as exc:
            logger.error(
                "Facebook token exchange failed (long-lived): status=%s body=%s",
                exc.response.status_code if exc.response is not None else "unknown",
                exc.response.text if exc.response is not None else "",
            )
            raise
        long_data = resp2.json()
        return long_data["access_token"], long_data.get("expires_in")


async def fetch_fb_profile(token: str) -> dict:
    async with AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{FB_GRAPH}/me",
            params={
                "fields": "id,name,email,picture.width(200).height(200)",
                "access_token": token,
            },
        )
        resp.raise_for_status()
        return resp.json()


def _fb_http_error_should_propagate(exc: HTTPStatusError) -> bool:
    """True when Graph returns an OAuth/session failure that callers must surface."""

    if exc.response is None:
        return False
    try:
        payload = exc.response.json()
    except ValueError:
        return False
    err = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(err, dict):
        return False
    if err.get("type") != "OAuthException":
        return False
    code = err.get("code")
    try:
        code_int = int(code) if code is not None else None
    except (TypeError, ValueError):
        code_int = None
    return code_int is not None and code_int in _FB_OAUTH_HARD_FAIL_CODES


def fb_engagement_counts(post: dict) -> tuple[int, int]:
    """Best-effort like/comment totals from Graph `likes` / `comments` summary objects."""

    likes_box = post.get("likes") or {}
    comments_box = post.get("comments") or {}
    raw_likes = likes_box.get("summary", {}).get("total_count", 0) if isinstance(likes_box, dict) else 0
    raw_comments = comments_box.get("summary", {}).get("total_count", 0) if isinstance(comments_box, dict) else 0
    try:
        return int(raw_likes or 0), int(raw_comments or 0)
    except (TypeError, ValueError):
        return 0, 0


def fb_caption_text(post: dict) -> str | None:
    """Prefer `message`; fall back to `story` (common for reshares / some media posts)."""

    for key in ("message", "story"):
        val = post.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return None


def fb_thumbnail_url(post: dict) -> str | None:
    """Preview image when Graph returns full_picture/picture on the post."""

    fp = post.get("full_picture")
    if isinstance(fp, str) and fp.strip():
        return fp.strip()
    pic = post.get("picture")
    if isinstance(pic, str) and pic.strip():
        return pic.strip()
    return None


async def _fetch_fb_posts_me_posts_edge(token: str, limit: int) -> tuple[list[dict], bool]:
    """GET /me/posts with progressively smaller field lists so mixed post types still return.

    Returns (posts, success). ``success`` is True when Meta returned HTTP 200 (including an empty list).
    On repeated non-auth failures, returns ([], False) so callers can try other strategies without
    surfacing a hard error to the user.
    """
    async with AsyncClient(timeout=15.0) as client:
        common_params = {
            "limit": limit,
            "access_token": token,
        }
        # Richest-first; drop fields that commonly break certain post types or permission shapes.
        field_sets = (
            "id,message,story,created_time,type,permalink_url,likes.summary(true),comments.summary(true),shares",
            "id,message,story,created_time,type,permalink_url,likes.summary(true),comments.summary(true)",
            "id,message,created_time,type,permalink_url,likes.summary(true),comments.summary(true),shares",
            "id,message,created_time,type,permalink_url,likes.summary(true),comments.summary(true)",
            "id,message,story,created_time,type,permalink_url,full_picture",
            "id,message,created_time,type,permalink_url,full_picture",
            "id,message,story,created_time,type,permalink_url",
            "id,message,created_time,type,permalink_url",
            "id,story,created_time,type,permalink_url",
            "id,created_time,type,permalink_url",
        )

        last_exc: HTTPStatusError | None = None
        for fields in field_sets:
            resp = await client.get(
                f"{FB_GRAPH}/me/posts",
                params={
                    "fields": fields,
                    **common_params,
                },
            )
            try:
                resp.raise_for_status()
            except HTTPStatusError as exc:
                last_exc = exc
                if _fb_http_error_should_propagate(exc):
                    raise
                status_code = exc.response.status_code if exc.response is not None else None
                if status_code in (401, 403):
                    raise
                logger.warning(
                    "Facebook /me/posts request failed; trying narrower fields "
                    "status=%s fields=%s body=%s",
                    status_code if status_code is not None else "unknown",
                    fields,
                    exc.response.text if exc.response is not None else "",
                )
                continue

            data = resp.json()
            return data.get("data", []) or [], True

        if last_exc is not None:
            logger.warning(
                "Facebook /me/posts: exhausted field fallbacks status=%s",
                last_exc.response.status_code if last_exc.response is not None else "unknown",
            )
        return [], False


async def _fetch_fb_posts_me_embedded_feed(token: str, limit: int) -> list[dict]:
    """
    New Pages experience workaround: nested `feed` on /me (see Meta developer community).
    Cap at 100 per Graph guidance for this pattern.
    """
    cap = min(max(limit, 1), 100)
    inner_sets = (
        "id,message,story,created_time,type,permalink_url,full_picture",
        "id,message,created_time,type,permalink_url,full_picture",
        "id,message,story,created_time,type,permalink_url",
        "id,message,created_time,type,permalink_url",
        "id,created_time,type,permalink_url",
    )
    async with AsyncClient(timeout=15.0) as client:
        last_exc: HTTPStatusError | None = None
        for inner in inner_sets:
            fields = f"feed.limit({cap}){{{inner}}}"
            resp = await client.get(
                f"{FB_GRAPH}/me",
                params={"fields": fields, "access_token": token},
            )
            try:
                resp.raise_for_status()
            except HTTPStatusError as exc:
                last_exc = exc
                if _fb_http_error_should_propagate(exc):
                    raise
                status_code = exc.response.status_code if exc.response is not None else None
                if status_code in (401, 403):
                    logger.warning(
                        "Facebook embedded /me feed forbidden status=%s",
                        status_code,
                    )
                    return []
                logger.warning(
                    "Facebook embedded /me feed failed; narrower fields status=%s body=%s",
                    status_code if status_code is not None else "unknown",
                    exc.response.text if exc.response is not None else "",
                )
                continue

            data = resp.json()
            feed = data.get("feed")
            if isinstance(feed, dict):
                return feed.get("data", []) or []
            return []

        if last_exc is not None:
            logger.warning("Facebook embedded /me feed: exhausted inner field fallbacks")
        return []


@dataclass
class FacebookPostsFetchResult:
    """Result of `fetch_fb_posts_detailed` (used by social-posts UX messaging)."""

    posts: list[dict]
    tried_npe_fallback: bool = False


async def fetch_fb_posts_detailed(token: str, limit: int = 50) -> FacebookPostsFetchResult:
    """
    Fetch the current user's Facebook posts.

    Uses /me/posts with progressive field fallbacks (so video/audio/mixed feeds still load when
    optional fields fail). If that path never succeeds, tries embedded ``feed`` on ``/me`` (New
    Pages experience and similar). Auth errors (401/403) still propagate.
    """
    posts, ok = await _fetch_fb_posts_me_posts_edge(token, limit)
    if ok:
        return FacebookPostsFetchResult(posts=posts[:limit], tried_npe_fallback=False)

    logger.info("Facebook /me/posts unavailable for this token; trying embedded /me feed fallback")
    embedded = await _fetch_fb_posts_me_embedded_feed(token, limit)
    if embedded:
        return FacebookPostsFetchResult(posts=embedded[:limit], tried_npe_fallback=True)
    logger.warning("Facebook embedded feed returned no posts for this account.")
    return FacebookPostsFetchResult(posts=[], tried_npe_fallback=True)


async def fetch_fb_posts(token: str, limit: int = 50) -> list[dict]:
    return (await fetch_fb_posts_detailed(token, limit)).posts


# ── Instagram Business Login ─────────────────────────────────────

# Default scopes for Instagram API with Instagram Login.
# Keep this minimal for initial connection; request additional scopes only if needed.
IG_SCOPES = "instagram_business_basic,instagram_business_manage_messages,instagram_business_manage_comments,instagram_business_content_publish,instagram_business_manage_insights"


def build_ig_authorize_url(user_id: str, settings: Settings, callback_url: str) -> str:
    app_id, _, _ = get_platform_app_credentials("instagram", settings)
    state = create_state(user_id, settings.META_STATE_SECRET)
    params = {
        "client_id": app_id,
        "redirect_uri": callback_url,
        "scope": IG_SCOPES,
        "response_type": "code",
        "state": state,
    }
    return f"{IG_OAUTH_AUTHORIZE}?{urlencode(params)}"


async def exchange_ig_code(code: str, callback_url: str, settings: Settings) -> tuple[str, int | None]:
    """Exchange code → short-lived → long-lived token."""
    app_id, app_secret, _ = get_platform_app_credentials("instagram", settings)
    async with AsyncClient(timeout=15.0) as client:
        # Short-lived
        resp = await client.post(
            IG_OAUTH_TOKEN,
            data={
                "client_id": app_id,
                "client_secret": app_secret,
                "grant_type": "authorization_code",
                "redirect_uri": callback_url,
                "code": code,
            },
        )
        try:
            resp.raise_for_status()
        except HTTPStatusError as exc:
            logger.error(
                "Instagram token exchange failed (short-lived): status=%s body=%s",
                exc.response.status_code if exc.response is not None else "unknown",
                exc.response.text if exc.response is not None else "",
            )
            raise
        short_data = resp.json()
        short_token = short_data["access_token"]

        # Long-lived (≈60 days)
        resp2 = await client.get(
            f"{IG_GRAPH}/access_token",
            params={
                "grant_type": "ig_exchange_token",
                "client_secret": app_secret,
                "access_token": short_token,
            },
        )
        try:
            resp2.raise_for_status()
        except HTTPStatusError as exc:
            logger.error(
                "Instagram token exchange failed (long-lived): status=%s body=%s",
                exc.response.status_code if exc.response is not None else "unknown",
                exc.response.text if exc.response is not None else "",
            )
            raise
        long_data = resp2.json()
        return long_data["access_token"], long_data.get("expires_in")


async def fetch_ig_profile(token: str) -> dict:
    async with AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{IG_GRAPH}/me",
            params={
                "fields": "id,username,account_type,media_count",
                "access_token": token,
            },
        )
        resp.raise_for_status()
        return resp.json()


async def fetch_ig_media(token: str, limit: int = 50) -> list[dict]:
    async with AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{IG_GRAPH}/me/media",
            params={
                "fields": "id,caption,media_type,media_url,thumbnail_url,timestamp,permalink",
                "limit": limit,
                "access_token": token,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", [])
