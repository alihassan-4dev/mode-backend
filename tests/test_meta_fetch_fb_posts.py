"""Tests for Facebook post fetch fallbacks (New Pages experience / 2069030)."""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.mark.asyncio
async def test_fetch_fb_posts_embedded_feed_after_2069030(monkeypatch: pytest.MonkeyPatch):
    from app.services import meta as meta_module

    req_posts = httpx.Request("GET", "https://graph.facebook.com/v21.0/me/posts")
    res_posts = httpx.Response(
        400,
        request=req_posts,
        json={
            "error": {
                "message": "Permissions error",
                "type": "OAuthException",
                "code": 200,
                "error_subcode": 2069030,
            }
        },
    )

    req_me = httpx.Request("GET", "https://graph.facebook.com/v21.0/me")
    res_me = httpx.Response(
        200,
        request=req_me,
        json={
            "feed": {
                "data": [
                    {
                        "id": "1_2",
                        "message": "from embedded feed",
                        "created_time": "2026-04-01T12:00:00+0000",
                        "type": "status",
                    }
                ]
            }
        },
    )

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, params=None):
            u = str(url)
            if "/me/posts" in u:
                return res_posts
            if "/me/accounts" in u:
                return httpx.Response(
                    200,
                    request=httpx.Request("GET", u),
                    json={"data": []},
                )
            fields = (params or {}).get("fields") or ""
            if u.rstrip("/").endswith("/me") and "feed.limit" in str(fields):
                return res_me
            return httpx.Response(200, request=httpx.Request("GET", u), json={})

    monkeypatch.setattr(meta_module, "AsyncClient", FakeAsyncClient)

    posts = await meta_module.fetch_fb_posts("user-token", limit=5)
    assert len(posts) == 1
    assert posts[0]["message"] == "from embedded feed"


@pytest.mark.asyncio
async def test_fetch_fb_posts_empty_after_2069030_and_empty_embedded(monkeypatch: pytest.MonkeyPatch):
    from app.services import meta as meta_module

    req_posts = httpx.Request("GET", "https://graph.facebook.com/v21.0/me/posts")
    res_posts = httpx.Response(
        400,
        request=req_posts,
        json={"error": {"message": "x", "code": 200, "error_subcode": 2069030}},
    )

    req_me = httpx.Request("GET", "https://graph.facebook.com/v21.0/me")
    res_me_empty = httpx.Response(200, request=req_me, json={"feed": {"data": []}})

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, params=None):
            u = str(url)
            if "/me/posts" in u:
                return res_posts
            fields = (params or {}).get("fields") or ""
            if u.rstrip("/").endswith("/me") and "feed.limit" in str(fields):
                return res_me_empty
            return httpx.Response(200, request=httpx.Request("GET", u), json={})

    monkeypatch.setattr(meta_module, "AsyncClient", FakeAsyncClient)

    posts = await meta_module.fetch_fb_posts("user-token", limit=10)
    assert posts == []


@pytest.mark.asyncio
async def test_fetch_fb_posts_me_posts_progressive_fields(monkeypatch: pytest.MonkeyPatch):
    """Later /me/posts field sets can succeed when richer fields return 400."""

    from app.services import meta as meta_module

    attempt = {"n": 0}
    req_posts = httpx.Request("GET", "https://graph.facebook.com/v21.0/me/posts")

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, params=None):
            u = str(url)
            if "/me/posts" not in u:
                return httpx.Response(200, request=httpx.Request("GET", u), json={})
            attempt["n"] += 1
            if attempt["n"] == 1:
                return httpx.Response(
                    400,
                    request=req_posts,
                    json={"error": {"message": "Invalid field", "code": 100}},
                )
            return httpx.Response(
                200,
                request=req_posts,
                json={
                    "data": [
                        {
                            "id": "p1",
                            "message": "video post",
                            "created_time": "2026-05-01T12:00:00+0000",
                            "type": "video",
                            "permalink_url": "https://www.facebook.com/story.php?story_fbid=p1",
                        }
                    ]
                },
            )

    monkeypatch.setattr(meta_module, "AsyncClient", FakeAsyncClient)

    posts = await meta_module.fetch_fb_posts("user-token", limit=5)
    assert len(posts) == 1
    assert posts[0]["type"] == "video"
    assert posts[0]["message"] == "video post"
