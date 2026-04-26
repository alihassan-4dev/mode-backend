"""
Facebook + Instagram OAuth helpers and Graph API client.

Facebook flow:
  1. build_fb_authorize_url  → user opens in browser
  2. exchange_fb_code        → short-lived token → long-lived token
  3. fetch_fb_profile        → /me with fields
  4. fetch_fb_posts          → /me/posts, then user-only fallback for New Pages experience:
     - /me?fields=feed.limit(n){...} (nested feed; works for some NPE profiles where /me/posts does not)

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


def _fb_http_error_subcode(exc: HTTPStatusError) -> int | None:
    if exc.response is None:
        return None
    try:
        payload = exc.response.json()
        err = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(err, dict) and err.get("error_subcode") is not None:
            return int(err["error_subcode"])
    except (ValueError, TypeError, KeyError):
        return None
    return None


async def _fetch_fb_posts_me_posts_edge(token: str, limit: int) -> list[dict]:
    """Classic path: GET /me/posts."""
    async with AsyncClient(timeout=15.0) as client:
        common_params = {
            "limit": limit,
            "access_token": token,
        }
        field_sets = [
            "id,message,created_time,type,permalink_url,likes.summary(true),comments.summary(true),shares",
            "id,message,created_time,type,permalink_url,likes.summary(true),comments.summary(true)",
        ]

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
                logger.warning(
                    "Facebook posts fetch failed; retrying with fallback fields "
                    "status=%s fields=%s body=%s",
                    exc.response.status_code if exc.response is not None else "unknown",
                    fields,
                    exc.response.text if exc.response is not None else "",
                )
                continue

            data = resp.json()
            return data.get("data", [])

        if last_exc is not None:
            raise last_exc
        return []


async def _fetch_fb_posts_me_embedded_feed(token: str, limit: int) -> list[dict]:
    """
    New Pages experience workaround: nested `feed` on /me (see Meta developer community).
    Cap at 100 per Graph guidance for this pattern.
    """
    cap = min(max(limit, 1), 100)
    # Keep embedded feed fields minimal. Some New Pages experience accounts reject
    # nested likes/comments on /me?fields=feed... with "(#100) nonexisting field".
    inner = "id,message,created_time,type,permalink_url"
    fields = f"feed.limit({cap}){{{inner}}}"
    async with AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{FB_GRAPH}/me",
            params={"fields": fields, "access_token": token},
        )
        try:
            resp.raise_for_status()
        except HTTPStatusError as exc:
            logger.warning(
                "Facebook embedded /me feed fetch failed status=%s body=%s",
                exc.response.status_code if exc.response is not None else "unknown",
                exc.response.text if exc.response is not None else "",
            )
            return []
        data = resp.json()
        feed = data.get("feed")
        if isinstance(feed, dict):
            return feed.get("data", []) or []
        return []


@dataclass
class FacebookPostsFetchResult:
    """Result of `fetch_fb_posts_detailed` (used by social-posts UX messaging)."""

    posts: list[dict]
    tried_npe_fallback: bool = False


async def fetch_fb_posts_detailed(token: str, limit: int = 50) -> FacebookPostsFetchResult:
    """
    Fetch the current user's Facebook posts.

    Tries /me/posts first. If Meta returns subcode 2069030 (New Pages experience / endpoint not
    supported), falls back to embedded `feed` on /me only (user-flow only).
    """
    try:
        posts = await _fetch_fb_posts_me_posts_edge(token, limit)
        return FacebookPostsFetchResult(posts=posts, tried_npe_fallback=False)
    except HTTPStatusError as exc:
        subcode = _fb_http_error_subcode(exc)
        if exc.response is not None and exc.response.status_code == 400 and subcode == 2069030:
            logger.info(
                "Facebook /me/posts unsupported (error_subcode=2069030); trying NPE-compatible fallbacks"
            )
            embedded = await _fetch_fb_posts_me_embedded_feed(token, limit)
            if embedded:
                return FacebookPostsFetchResult(posts=embedded[:limit], tried_npe_fallback=True)
            logger.warning(
                "Facebook user-flow fallback returned no posts for this account."
            )
            return FacebookPostsFetchResult(posts=[], tried_npe_fallback=True)
        raise


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
