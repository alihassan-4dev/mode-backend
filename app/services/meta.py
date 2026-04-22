"""
Facebook + Instagram OAuth helpers and Graph API client.

Facebook flow:
  1. build_fb_authorize_url  → user opens in browser
  2. exchange_fb_code        → short-lived token → long-lived token
  3. fetch_fb_profile        → /me with fields
  4. fetch_fb_posts          → /me/posts

Instagram Basic Display flow:
  1. build_ig_authorize_url
  2. exchange_ig_code        → short → long-lived token
  3. fetch_ig_profile        → /me
  4. fetch_ig_media          → /me/media
"""

from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import urlencode

import httpx

from app.core.config import Settings

FB_GRAPH = "https://graph.facebook.com/v21.0"
FB_OAUTH_DIALOG = "https://www.facebook.com/v21.0/dialog/oauth"
IG_OAUTH_AUTHORIZE = "https://api.instagram.com/oauth/authorize"
IG_OAUTH_TOKEN = "https://api.instagram.com/oauth/access_token"
IG_GRAPH = "https://graph.instagram.com"

STATE_MAX_AGE = 900  # 15 minutes


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

FB_SCOPES = "public_profile,email,user_posts,user_likes"


def build_fb_authorize_url(user_id: str, settings: Settings, callback_url: str) -> str:
    state = create_state(user_id, settings.META_STATE_SECRET)
    params = {
        "client_id": settings.META_APP_ID,
        "redirect_uri": callback_url,
        "scope": FB_SCOPES,
        "response_type": "code",
        "state": state,
    }
    return f"{FB_OAUTH_DIALOG}?{urlencode(params)}"


async def exchange_fb_code(code: str, callback_url: str, settings: Settings) -> tuple[str, int | None]:
    """Exchange auth code → short-lived → long-lived token. Returns (token, expires_in_seconds)."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        # Short-lived token
        resp = await client.get(
            f"{FB_GRAPH}/oauth/access_token",
            params={
                "client_id": settings.META_APP_ID,
                "client_secret": settings.META_APP_SECRET,
                "redirect_uri": callback_url,
                "code": code,
            },
        )
        resp.raise_for_status()
        short_data = resp.json()
        short_token = short_data["access_token"]

        # Long-lived token (≈60 days)
        resp2 = await client.get(
            f"{FB_GRAPH}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": settings.META_APP_ID,
                "client_secret": settings.META_APP_SECRET,
                "fb_exchange_token": short_token,
            },
        )
        resp2.raise_for_status()
        long_data = resp2.json()
        return long_data["access_token"], long_data.get("expires_in")


async def fetch_fb_profile(token: str) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{FB_GRAPH}/me",
            params={
                "fields": "id,name,email,picture.width(200).height(200)",
                "access_token": token,
            },
        )
        resp.raise_for_status()
        return resp.json()


async def fetch_fb_posts(token: str, limit: int = 50) -> list[dict]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{FB_GRAPH}/me/posts",
            params={
                "fields": "id,message,created_time,type,likes.summary(true),comments.summary(true),shares",
                "limit": limit,
                "access_token": token,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", [])


# ── Instagram Basic Display ──────────────────────────────────────

IG_SCOPES = "user_profile,user_media"


def build_ig_authorize_url(user_id: str, settings: Settings, callback_url: str) -> str:
    state = create_state(user_id, settings.META_STATE_SECRET)
    params = {
        "client_id": settings.META_APP_ID,
        "redirect_uri": callback_url,
        "scope": IG_SCOPES,
        "response_type": "code",
        "state": state,
    }
    return f"{IG_OAUTH_AUTHORIZE}?{urlencode(params)}"


async def exchange_ig_code(code: str, callback_url: str, settings: Settings) -> tuple[str, int | None]:
    """Exchange code → short-lived → long-lived token."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        # Short-lived
        resp = await client.post(
            IG_OAUTH_TOKEN,
            data={
                "client_id": settings.META_APP_ID,
                "client_secret": settings.META_APP_SECRET,
                "grant_type": "authorization_code",
                "redirect_uri": callback_url,
                "code": code,
            },
        )
        resp.raise_for_status()
        short_data = resp.json()
        short_token = short_data["access_token"]

        # Long-lived (≈60 days)
        resp2 = await client.get(
            f"{IG_GRAPH}/access_token",
            params={
                "grant_type": "ig_exchange_token",
                "client_secret": settings.META_APP_SECRET,
                "access_token": short_token,
            },
        )
        resp2.raise_for_status()
        long_data = resp2.json()
        return long_data["access_token"], long_data.get("expires_in")


async def fetch_ig_profile(token: str) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
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
    async with httpx.AsyncClient(timeout=15.0) as client:
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
